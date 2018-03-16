import os

from datasets.pascal_voc_reader import pascal_inputs, TRAIN_DIR
from segs.factory import get_net
from tf_ops.benchmarks import mAP
from tf_ops.wrap_ops import *

arg_scope = tf.contrib.framework.arg_scope

LOSS_COLLECTIONS = tf.GraphKeys.LOSSES

# pre settings
tf.app.flags.DEFINE_string('data_dir', TRAIN_DIR, 'where training set is put')
tf.app.flags.DEFINE_multi_integer('reshape_size', [224, 224], 'reshape')
tf.app.flags.DEFINE_string('net_name', 'fcn8', 'which segmentation net to use')
tf.app.flags.DEFINE_integer('num_classes', 21, '#classes')

# learning configs
tf.app.flags.DEFINE_integer('epoch_nums', 1, 'epoch_nums')
tf.app.flags.DEFINE_integer('batch_size', 2, 'batch size')
tf.app.flags.DEFINE_float('weight_learning_rate', 1e-3, 'weight learning rate')
tf.app.flags.DEFINE_float('bias_learning_rate', None, 'bias learning rate')
tf.app.flags.DEFINE_float('clip_grad_by_norm', 5, 'clip_grad_by_norm')
tf.app.flags.DEFINE_float('learning_decay', 0.99, 'learning rate decay')
tf.app.flags.DEFINE_float('momentum', 0.99, 'momentum')

# deploy configs
tf.app.flags.DEFINE_string('store_device', 'cpu', 'where to place the variables')
tf.app.flags.DEFINE_string('run_device', 'cpu', 'where to run the models')
tf.app.flags.DEFINE_float('gpu_fraction', 0.8, 'gpu memory fraction')
tf.app.flags.DEFINE_boolean('allow_growth', True, 'allow memory growth')

# regularization
tf.app.flags.DEFINE_float('weight_reg_scale', 1e-2, 'weight regularization scale')
tf.app.flags.DEFINE_string('weight_reg_func', 'l2', 'use which func to regularize weight')
tf.app.flags.DEFINE_float('bias_reg_scale', None, 'bias regularization scale')
tf.app.flags.DEFINE_string('bias_reg_func', None, 'use which func to regularize bias')

FLAGS = tf.app.flags.FLAGS

if FLAGS.reshape_size is None and FLAGS.batch_size != 1:
    assert 0, 'Can''t Stack Images Of Different Shapes, Please Speicify Reshape Size!'

if FLAGS.store_device.lower() == 'cpu':
    device = 'cpu'
elif FLAGS.store_device in ['0', '1', '2', '3']:
    device = FLAGS.device
else:
    device = None

config = tf.ConfigProto(log_device_placement=True)
if set(FLAGS.run_device).issubset({'0', '1', '2', '3'}):
    os.environ['CUDA_VISIBLE_DEVICES'] = ''.join(FLAGS.run_device)
    config.gpu_options.allow_growth = FLAGS.allow_growth
    config.gpu_options.per_process_gpu_memory_fraction = FLAGS.gpu_fraction

# set up step
sess = tf.Session(config=config)
global_step = tf.Variable(0, trainable=False, name='global_step')

# read data
name_batch, image_batch, label_batch = pascal_inputs(
    dir=FLAGS.data_dir, batch_size=FLAGS.batch_size, num_epochs=1, reshape_size=FLAGS.reshape_size)

# inference
weight_reg = regularizer(mode=FLAGS.weight_reg_func, scale=FLAGS.weight_reg_scale)
bias_reg = regularizer(mode=FLAGS.bias_reg_func, scale=FLAGS.bias_reg_scale)

net = get_net(FLAGS.net_name)
score_map, endpoints = net(image_batch, num_classes=FLAGS.num_classes,
                           weight_init=None, weight_reg=weight_reg,
                           bias_init=tf.zeros_initializer, bias_reg=bias_reg, device=device)

# solve for mAP and loss
class_map = arg_max(score_map, axis=3, name='class_map')
pixel_acc = mAP(class_map, label_batch)

# calculate loss
loss = softmax_with_logits(score_map, label_batch)
mean_loss = tf.reduce_mean(loss, name='mean_loss')
reg_losses = tf.get_collection(tf.GraphKeys.REGULARIZATION_LOSSES)
reg_loss = tf.reduce_sum(reg_losses)
total_loss = mean_loss + reg_loss

# set up optimizer
decay_learning_rate = tf.train.exponential_decay(FLAGS.weight_learning_rate, global_step,
                                                 decay_steps=100000, decay_rate=0.96, staircase=True)
optimizer = tf.train.MomentumOptimizer(learning_rate=decay_learning_rate, momentum=FLAGS.momentum)
ratio = (FLAGS.weight_learning_rate / FLAGS.bias_learning_rate) \
    if FLAGS.bias_learning_rate is not None else 2

# solve for gradients
weight_vars = tf.get_collection(weight_collections)
bias_vars = tf.get_collection(bias_collections)

weight_grads = optimizer.compute_gradients(total_loss, weight_vars)
weight_grads = [(tf.clip_by_norm(grad, clip_norm=FLAGS.clip_grad_by_norm), var)
                for grad, var in weight_grads if grad is not None]

bias_grads = optimizer.compute_gradients(total_loss, bias_vars)
bias_grads = [(tf.clip_by_norm(ratio * grad, clip_norm=FLAGS.clip_grad_by_norm), var)
              for grad, var in bias_grads if grad is not None]

# set up train operation
train_op = tf.group(
    optimizer.apply_gradients(weight_grads),
    optimizer.apply_gradients(bias_grads)
)

# start training
try:
    while True:  # train until OutOfRangeError
        sess.run(tf.global_variables_initializer())
        batch_mAP, batch_tloss, batch_rloss, _ = \
            sess.run([pixel_acc, mean_loss, reg_loss, train_op])

        print(batch_mAP, batch_tloss, batch_rloss)
except tf.errors.OutOfRangeError:
    print('Done training')
    
