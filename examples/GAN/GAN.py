#!/usr/bin/env python
# -*- coding: utf-8 -*-
# File: GAN.py
# Author: Yuxin Wu <ppwwyyxxc@gmail.com>

import tensorflow as tf
import numpy as np
from tensorpack import (TowerTrainer,
                        ModelDescBase, DataFlow, StagingInput)
from tensorpack.tfutils.tower import TowerContext, TowerFuncWrapper
from tensorpack.graph_builder import DataParallelBuilder, LeastLoadedDeviceSetter
from tensorpack.tfutils.summary import add_moving_summary
from tensorpack.utils.argtools import memoized


class GANModelDesc(ModelDescBase):
    def collect_variables(self, g_scope='gen', d_scope='discrim'):
        """
        Assign self.g_vars to the parameters under scope `g_scope`,
        and same with self.d_vars.
        """
        self.g_vars = tf.get_collection(tf.GraphKeys.TRAINABLE_VARIABLES, g_scope)
        assert self.g_vars
        self.d_vars = tf.get_collection(tf.GraphKeys.TRAINABLE_VARIABLES, d_scope)
        assert self.d_vars

    def build_losses(self, logits_real, logits_fake):
        """D and G play two-player minimax game with value function V(G,D)

          min_G max _D V(D, G) = IE_{x ~ p_data} [log D(x)] + IE_{z ~ p_fake} [log (1 - D(G(z)))]

        Args:
            logits_real (tf.Tensor): discrim logits from real samples
            logits_fake (tf.Tensor): discrim logits from fake samples produced by generator
        """
        with tf.name_scope("GAN_loss"):
            score_real = tf.sigmoid(logits_real)
            score_fake = tf.sigmoid(logits_fake)
            tf.summary.histogram('score-real', score_real)
            tf.summary.histogram('score-fake', score_fake)

            with tf.name_scope("discrim"):
                d_loss_pos = tf.reduce_mean(tf.nn.sigmoid_cross_entropy_with_logits(
                    logits=logits_real, labels=tf.ones_like(logits_real)), name='loss_real')
                d_loss_neg = tf.reduce_mean(tf.nn.sigmoid_cross_entropy_with_logits(
                    logits=logits_fake, labels=tf.zeros_like(logits_fake)), name='loss_fake')

                d_pos_acc = tf.reduce_mean(tf.cast(score_real > 0.5, tf.float32), name='accuracy_real')
                d_neg_acc = tf.reduce_mean(tf.cast(score_fake < 0.5, tf.float32), name='accuracy_fake')

                d_accuracy = tf.add(.5 * d_pos_acc, .5 * d_neg_acc, name='accuracy')
                self.d_loss = tf.add(.5 * d_loss_pos, .5 * d_loss_neg, name='loss')

            with tf.name_scope("gen"):
                self.g_loss = tf.reduce_mean(tf.nn.sigmoid_cross_entropy_with_logits(
                    logits=logits_fake, labels=tf.ones_like(logits_fake)), name='loss')
                g_accuracy = tf.reduce_mean(tf.cast(score_fake > 0.5, tf.float32), name='accuracy')

            add_moving_summary(self.g_loss, self.d_loss, d_accuracy, g_accuracy)

    @memoized
    def get_optimizer(self):
        return self._get_optimizer()


class GANTrainer(TowerTrainer):
    def __init__(self, input, model):
        super(GANTrainer, self).__init__()
        assert isinstance(model, GANModelDesc), model
        inputs_desc = model.get_inputs_desc()
        cbs = input.setup(inputs_desc)

        # we need to set towerfunc because it's a TowerTrainer,
        # and only TowerTrainer supports automatic graph creation for inference during training.
        tower_func = TowerFuncWrapper(model.build_graph, inputs_desc)
        with TowerContext('', is_training=True):
            tower_func(*input.get_input_tensors())
        opt = model.get_optimizer()

        # by default, run one d_min after one g_min
        with tf.name_scope('optimize'):
            g_min = opt.minimize(model.g_loss, var_list=model.g_vars, name='g_op')
            with tf.control_dependencies([g_min]):
                d_min = opt.minimize(model.d_loss, var_list=model.d_vars, name='d_op')
        self.train_op = d_min
        self.set_tower_func(tower_func)

        for cb in cbs:
            self.register_callback(cb)


class SeparateGANTrainer(TowerTrainer):
    """ A GAN trainer which runs two optimization ops with a certain ratio."""
    def __init__(self, input, model, d_period=1, g_period=1):
        """
        Args:
            d_period(int): period of each d_opt run
            g_period(int): period of each g_opt run
        """
        super(SeparateGANTrainer, self).__init__()
        self._d_period = int(d_period)
        self._g_period = int(g_period)
        assert min(d_period, g_period) == 1

        cbs = input.setup(model.get_inputs_desc())
        tower_func = TowerFuncWrapper(model.build_graph, model.get_inputs_desc())
        with TowerContext('', is_training=True):
            tower_func(*input.get_input_tensors())

        opt = model.get_optimizer()
        with tf.name_scope('optimize'):
            self.d_min = opt.minimize(
                model.d_loss, var_list=model.d_vars, name='d_min')
            self.g_min = opt.minimize(
                model.g_loss, var_list=model.g_vars, name='g_min')

        self.set_tower_func(tower_func)
        for cb in cbs:
            self.register_callback(cb)

    def run_step(self):
        if self.global_step % (self._d_period) == 0:
            self.hooked_sess.run(self.d_min)
        if self.global_step % (self._g_period) == 0:
            self.hooked_sess.run(self.g_min)


class MultiGPUGANTrainer(TowerTrainer):
    """
    A replacement of GANTrainer (optimize d and g one by one) with multi-gpu support.
    """
    def __init__(self, nr_gpu, input, model):
        super(MultiGPUGANTrainer, self).__init__()
        assert nr_gpu > 1
        raw_devices = ['/gpu:{}'.format(k) for k in range(nr_gpu)]

        # setup input
        input = StagingInput(input, list(range(nr_gpu)))
        cbs = input.setup(model.get_inputs_desc())

        # build the graph
        def get_cost(*inputs):
            model.build_graph(*inputs)
            return [model.d_loss, model.g_loss]
        tower_func = TowerFuncWrapper(get_cost, model.get_inputs_desc())
        devices = [LeastLoadedDeviceSetter(d, raw_devices) for d in raw_devices]
        cost_list = DataParallelBuilder.build_on_towers(
            list(range(nr_gpu)),
            lambda: tower_func(*input.get_input_tensors()),
            devices)
        # simply average the cost. It might get faster to average the gradients
        with tf.name_scope('optimize'):
            d_loss = tf.add_n([x[0] for x in cost_list]) * (1.0 / nr_gpu)
            g_loss = tf.add_n([x[1] for x in cost_list]) * (1.0 / nr_gpu)

            opt = model.get_optimizer()
            # run one d_min after one g_min
            g_min = opt.minimize(g_loss, var_list=model.g_vars,
                                 colocate_gradients_with_ops=True, name='g_op')
            with tf.control_dependencies([g_min]):
                d_min = opt.minimize(d_loss, var_list=model.d_vars,
                                     colocate_gradients_with_ops=True, name='d_op')
        self.train_op = d_min
        self.set_tower_func(tower_func)
        for cb in cbs:
            self.register_callback(cb)


class RandomZData(DataFlow):
    def __init__(self, shape):
        super(RandomZData, self).__init__()
        self.shape = shape

    def get_data(self):
        while True:
            yield [np.random.uniform(-1, 1, size=self.shape)]
