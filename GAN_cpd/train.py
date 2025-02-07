# Copyright 2018, Tero Karras, NVIDIA CORPORATION
#
# Copyright (c) 2018, NVIDIA CORPORATION. All rights reserved.
#
# This work is licensed under the Creative Commons Attribution-NonCommercial
# 4.0 International License. To view a copy of this license, visit
# http://creativecommons.org/licenses/by-nc/4.0/ or send a letter to
# Creative Commons, PO Box 1866, Mountain View, CA 94042, USA.
#
# Adapted from the original implementation by Tero Karras.
# Source https://github.com/tkarras/progressive_growing_of_gans

import os
import time
import numpy as np
import tensorflow as tf
import sys
import config
import tfutil
import dataset
import misc
import argparse
from fid_metric import compute_fid
from csv import writer 
#----------------------------------------------------------------------------
# Choose the size and contents of the image snapshot grids that are exported
# periodically during training.

def setup_snapshot_image_grid(G, training_set,
    size    = '1080p',      # '1080p' = to be viewed on 1080p display, '4k' = to be viewed on 4k display.
    layout  = 'random'):    # 'random' = grid contents are selected randomly, 'row_per_class' = each row corresponds to one class label.

    # Select size.
    gw = 1; gh = 1
    if size == '1080p':
        gw = np.clip(1920 // G.output_shape[3], 3, 32)
        gh = np.clip(1080 // G.output_shape[2], 2, 32)
    if size == '4k':
        gw = np.clip(3840 // G.output_shape[3], 7, 32)
        gh = np.clip(2160 // G.output_shape[2], 4, 32)

    # Fill in reals and labels.
    reals = np.zeros([gw * gh] + training_set.shape, dtype=training_set.dtype)
    labels = np.zeros([gw * gh, training_set.label_size], dtype=training_set.label_dtype)
    for idx in range(gw * gh):
        x = idx % gw; y = idx // gw
        while True:
            real, label = training_set.get_minibatch_np(1)
            if layout == 'row_per_class' and training_set.label_size > 0:
                if label[0, y % training_set.label_size] == 0.0:
                    continue
            reals[idx] = real[0]
            labels[idx] = label[0]
            break

    # Generate latents.
    latents = misc.random_latents(gw * gh, G)
    return (gw, gh), reals, labels, latents

#----------------------------------------------------------------------------
# Just-in-time processing of training images before feeding them to the networks.

def process_reals(x, lod, mirror_augment, drange_data, drange_net):
    with tf.name_scope('ProcessReals'):
        with tf.name_scope('DynamicRange'):
            x = tf.cast(x, tf.float32)
            x = misc.adjust_dynamic_range(x, drange_data, drange_net)
        if mirror_augment:
            with tf.name_scope('MirrorAugment'):
                s = tf.shape(x)
                mask = tf.random_uniform([s[0], 1, 1, 1], 0.0, 1.0)
                mask = tf.tile(mask, [1, s[1], s[2], s[3]])
                x = tf.where(mask < 0.5, x, tf.reverse(x, axis=[3]))
        with tf.name_scope('FadeLOD'): # Smooth crossfade between consecutive levels-of-detail.
            s = tf.shape(x)
            y = tf.reshape(x, [-1, s[1], s[2]//2, 2, s[3]//2, 2])
            y = tf.reduce_mean(y, axis=[3, 5], keepdims=True)
            y = tf.tile(y, [1, 1, 1, 2, 1, 2])
            y = tf.reshape(y, [-1, s[1], s[2], s[3]])
            x = tfutil.lerp(x, y, lod - tf.floor(lod))
        with tf.name_scope('UpscaleLOD'): # Upscale to match the expected input/output size of the networks.
            s = tf.shape(x)
            factor = tf.cast(2 ** tf.floor(lod), tf.int32)
            x = tf.reshape(x, [-1, s[1], s[2], 1, s[3], 1])
            x = tf.tile(x, [1, 1, 1, factor, 1, factor])
            x = tf.reshape(x, [-1, s[1], s[2] * factor, s[3] * factor])
        return x

#----------------------------------------------------------------------------
# Class for evaluating and storing the values of time-varying training parameters.

class TrainingSchedule:
    def __init__(
        self,
        cur_nimg,
        training_set,
        lod_initial_resolution  = 4,        # Image resolution used at the beginning.
        lod_training_kimg       = 600,      # Thousands of real images to show before doubling the resolution.
        lod_transition_kimg     = 600,      # Thousands of real images to show when fading in new layers.
        minibatch_base          = 16,       # Maximum minibatch size, divided evenly among GPUs.
        minibatch_dict          = {},       # Resolution-specific overrides.
        max_minibatch_per_gpu   = {},       # Resolution-specific maximum minibatch size per GPU.
        G_lrate_base            = 0.001,    # Learning rate for the generator.
        G_lrate_dict            = {},       # Resolution-specific overrides.
        D_lrate_base            = 0.001,    # Learning rate for the discriminator.
        D_lrate_dict            = {},       # Resolution-specific overrides.
        C_lrate_base            = 0.001,    # Learning rate for the discriminator.
        C_lrate_dict            = {},       # Resolution-specific overrides.    
        tick_kimg_base          = 160,      # Default interval of progress snapshots.
        tick_kimg_dict          = {4: 160, 8:140, 16:120, 32:100, 64:80, 128:60, 256:40, 512:20, 1024:10}): # Resolution-specific overrides.

        # Training phase.
        self.kimg = cur_nimg / 1000.0
        phase_dur = lod_training_kimg + lod_transition_kimg
        phase_idx = int(np.floor(self.kimg / phase_dur)) if phase_dur > 0 else 0
        phase_kimg = self.kimg - phase_idx * phase_dur

        # Level-of-detail and resolution.
        self.lod = training_set.resolution_log2
        self.lod -= np.floor(np.log2(lod_initial_resolution))
        self.lod -= phase_idx
        if lod_transition_kimg > 0:
            self.lod -= max(phase_kimg - lod_training_kimg, 0.0) / lod_transition_kimg
        self.lod = max(self.lod, 0.0)
        self.resolution = 2 ** (training_set.resolution_log2 - int(np.floor(self.lod)))

        # Minibatch size.
        self.minibatch = minibatch_dict.get(self.resolution, minibatch_base)
        self.minibatch -= self.minibatch % config.num_gpus
        if self.resolution in max_minibatch_per_gpu:
            self.minibatch = min(self.minibatch, max_minibatch_per_gpu[self.resolution] * config.num_gpus)

        # Other parameters.
        self.G_lrate = G_lrate_dict.get(self.resolution, G_lrate_base)
        self.D_lrate = D_lrate_dict.get(self.resolution, D_lrate_base)
        self.tick_kimg = tick_kimg_dict.get(self.resolution, tick_kimg_base)

#----------------------------------------------------------------------------
# Main training script.
# To run, comment/uncomment appropriate lines in config.py and launch train.py.

def train_progressive_gan(
    G_smoothing             = 0.999,        # Exponential running average of generator weights.
    D_repeats               = 1,            # How many times the discriminator is trained per G iteration.
    minibatch_repeats       = 4,            # Number of minibatches to run before adjusting training parameters.
    reset_opt_for_new_lod   = True,         # Reset optimizer internal state (e.g. Adam moments) when new layers are introduced?
    total_kimg              = 15000,        # Total length of the training, measured in thousands of real images.
    mirror_augment          = False,        # Enable mirror augment?
    drange_net              = [-1,1],       # Dynamic range used when feeding image data to the networks.
    image_snapshot_ticks    = 1,            # How often to export image snapshots?
    network_snapshot_ticks  = 10,           # How often to export network snapshots?
    compute_fid_score       = False,        # Compute FID during training once sched.lod=0.0 
    minimum_fid_kimg        = 0,            # Compute FID after 
    fid_snapshot_ticks      = 1,            # How often to compute FID
    fid_patience            = 2,            # When to end training based on FID
    save_tf_graph           = False,        # Include full TensorFlow computation graph in the tfevents file?
    save_weight_histograms  = False,        # Include weight histograms in the tfevents file?
    resume_run_id           = None,         # Run ID or network pkl to resume training from, None = start from scratch.
    resume_snapshot         = None,         # Snapshot index to resume training from, None = autodetect.
    resume_kimg             = 0.0,          # Assumed training progress at the beginning. Affects reporting and training schedule.
    resume_time             = 0.0,         # Assumed wallclock time at the beginning. Affects reporting.
    result_subdir           = "./"):
    maintenance_start_time = time.time()
    training_set = dataset.load_dataset(data_dir=config.data_dir, verbose=True, **config.dataset)

    # Construct networks.
    with tf.device('/gpu:0'):
        if resume_run_id is not None:
            network_pkl = misc.locate_network_pkl(resume_run_id, resume_snapshot)
            print('Loading networks from "%s"...' % network_pkl, flush=True)
            G, D, Gs = misc.load_pkl(network_pkl)
        else:
            print('Constructing networks...', flush=True)
            G = tfutil.Network('G', num_channels=training_set.shape[0], resolution=training_set.shape[1], label_size=training_set.label_size, **config.G)
            D = tfutil.Network('D', num_channels=training_set.shape[0], resolution=training_set.shape[1], label_size=training_set.label_size, **config.D)
            Gs = G.clone('Gs')
        Gs_update_op = Gs.setup_as_moving_average_of(G, beta=G_smoothing)
    G.print_layers(); D.print_layers()

    print('Building TensorFlow graph...', flush=True)
    with tf.name_scope('Inputs'):
        lod_in          = tf.placeholder(tf.float32, name='lod_in', shape=[])
        lrate_in        = tf.placeholder(tf.float32, name='lrate_in', shape=[])
        minibatch_in    = tf.placeholder(tf.int32, name='minibatch_in', shape=[])
        minibatch_split = minibatch_in // config.num_gpus
        reals, labels   = training_set.get_minibatch_tf()
        reals_split     = tf.split(reals, config.num_gpus)
        labels_split    = tf.split(labels, config.num_gpus)
    G_opt = tfutil.Optimizer(name='TrainG', learning_rate=lrate_in, **config.G_opt)
    D_opt = tfutil.Optimizer(name='TrainD', learning_rate=lrate_in, **config.D_opt)

    for gpu in range(config.num_gpus):
        with tf.name_scope('GPU%d' % gpu), tf.device('/gpu:%d' % gpu):
            G_gpu = G if gpu == 0 else G.clone(G.name + '_shadow')
            D_gpu = D if gpu == 0 else D.clone(D.name + '_shadow')
            reals_gpu = process_reals(reals_split[gpu], lod_in, mirror_augment, training_set.dynamic_range, drange_net)
            labels_gpu = labels_split[gpu]
            with tf.name_scope('G_loss'):
                G_loss = tfutil.call_func_by_name(G=G_gpu, D=D_gpu, opt=G_opt, training_set=training_set, minibatch_size=minibatch_split, **config.G_loss)
            with tf.name_scope('D_loss'):
                D_loss = tfutil.call_func_by_name(G=G_gpu, D=D_gpu, opt=D_opt, training_set=training_set, minibatch_size=minibatch_split, reals=reals_gpu, labels=labels_gpu, **config.D_loss)

            G_opt.register_gradients(tf.reduce_mean(G_loss), G_gpu.trainables)
            D_opt.register_gradients(tf.reduce_mean(D_loss), D_gpu.trainables)

    G_train_op = G_opt.apply_updates()
    D_train_op = D_opt.apply_updates()

    print('Setting up snapshot image grid...', flush=True)
    grid_size, grid_reals, grid_labels, grid_latents = setup_snapshot_image_grid(G, training_set, **config.grid)
    sched = TrainingSchedule(total_kimg * 1000, training_set, **config.sched)
    grid_fakes = Gs.run(grid_latents, grid_labels, minibatch_size=sched.minibatch//config.num_gpus)

    print('Setting up result dir...', flush=True)
    result_subdir = misc.create_result_subdir(config.result_dir, config.desc)
    misc.save_image_grid(grid_reals, os.path.join(result_subdir, 'reals.png'), drange=training_set.dynamic_range, grid_size=grid_size)
    misc.save_image_grid(grid_fakes, os.path.join(result_subdir, 'fakes%06d.png' % 0), drange=drange_net, grid_size=grid_size)
    summary_log = tf.summary.FileWriter(result_subdir)
    if save_tf_graph:
        summary_log.add_graph(tf.get_default_graph())
    if save_weight_histograms:
        G.setup_weight_histograms(); D.setup_weight_histograms()

    print('Training...', flush=True)
    # FID patience parameters:
    fid_list = []
    fid_steps = 0
    fid_stop = False
    fid_patience_step = 0

    cur_nimg = int(resume_kimg * 1000)
    cur_tick = 0
    tick_start_nimg = cur_nimg
    tick_start_time = time.time()
    train_start_time = tick_start_time - resume_time
    prev_lod = -1.0
    while cur_nimg < total_kimg * 1000:

        # Choose training parameters and configure training ops.
        sched = TrainingSchedule(cur_nimg, training_set, **config.sched)
        training_set.configure(sched.minibatch, sched.lod)
        if reset_opt_for_new_lod:
            if np.floor(sched.lod) != np.floor(prev_lod) or np.ceil(sched.lod) != np.ceil(prev_lod):
                G_opt.reset_optimizer_state(); D_opt.reset_optimizer_state()
        prev_lod = sched.lod

        # Run training ops.
        for repeat in range(minibatch_repeats):
            for _ in range(D_repeats):
                tfutil.run([D_train_op, Gs_update_op], {lod_in: sched.lod,lrate_in: sched.D_lrate, minibatch_in: sched.minibatch})
                cur_nimg += sched.minibatch
            tfutil.run([G_train_op], {lod_in: sched.lod,lrate_in: sched.G_lrate, minibatch_in: sched.minibatch})

        # Perform maintenance tasks once per tick.
        done = (cur_nimg >= total_kimg * 1000)
        if cur_nimg >= tick_start_nimg + sched.tick_kimg * 1000 or done:
            cur_tick += 1
            cur_time = time.time()
            tick_kimg = (cur_nimg - tick_start_nimg) / 1000.0
            tick_start_nimg = cur_nimg
            tick_time = cur_time - tick_start_time
            total_time = cur_time - train_start_time
            maintenance_time = tick_start_time - maintenance_start_time
            maintenance_start_time = cur_time

            if (compute_fid_score == True) and (cur_tick%fid_snapshot_ticks == 0)  and (sched.lod==0.0) and (cur_nimg>=minimum_fid_kimg * 1000):
                fid = compute_fid(Gs=Gs,minibatch_size=sched.minibatch, 
                    dataset_obj=training_set, iter_number=cur_nimg/1000, 
                    lod = 0.0, num_images=10000, printing=False)
                fid_list.append(fid)

            # Report progress without FID.
            print('tick %-5d kimg %-8.1f lod %-5.2f minibatch %-4d time %-12s sec/tick %-7.1f sec/kimg %-7.2f maintenance %.1f' % (
                tfutil.autosummary('Progress/tick', cur_tick),
                tfutil.autosummary('Progress/kimg', cur_nimg / 1000.0),
                tfutil.autosummary('Progress/lod', sched.lod),
                tfutil.autosummary('Progress/minibatch', sched.minibatch),
                misc.format_time(tfutil.autosummary('Timing/total_sec', total_time)),
                tfutil.autosummary('Timing/sec_per_tick', tick_time),
                tfutil.autosummary('Timing/sec_per_kimg', tick_time / tick_kimg),
                tfutil.autosummary('Timing/maintenance_sec', maintenance_time)), flush=True)
            tfutil.autosummary('Timing/total_hours', total_time / (60.0 * 60.0))
            tfutil.autosummary('Timing/total_days', total_time / (24.0 * 60.0 * 60.0))
            tfutil.save_summaries(summary_log, cur_nimg)

            # Save image snapshots.
            if cur_tick % image_snapshot_ticks == 0 or done:
                grid_fakes = Gs.run(grid_latents, grid_labels, minibatch_size=sched.minibatch//config.num_gpus)
                misc.save_image_grid(grid_fakes, os.path.join(result_subdir, 'fakes%06d.png' % (cur_nimg // 1000)), drange=drange_net, grid_size=grid_size)

            # Save network snapshots
            if cur_tick % network_snapshot_ticks == 0 or done or (compute_fid_score == True) and (cur_tick%fid_snapshot_ticks == 0) and (cur_nimg>=minimum_fid_kimg * 1000):
                misc.save_pkl((G, D, Gs), os.path.join(result_subdir, 'network-snapshot-%06d.pkl' % (cur_nimg // 1000)))

            # End training based on FID patience 
            if (compute_fid_score == True) and (cur_tick%fid_snapshot_ticks == 0)  and (sched.lod==0.0) and (cur_nimg>=minimum_fid_kimg * 1000):
                fid_patience_step += 1
                if len(fid_list) == 1:
                    fid_patience_step = 0
                    misc.save_pkl((G, D, Gs), os.path.join(result_subdir, 'network-final-full-conv.pkl'))
                    print("Save network-final-full-conv for FID: %.3f at kimg %-8.1f." %(fid_list[-1], cur_nimg//1000), flush=True)
                else: 
                    if fid_list[-1] < np.min(fid_list[:-1]):
                        fid_patience_step = 0
                        misc.save_pkl((G, D, Gs), os.path.join(result_subdir, 'network-final-full-conv.pkl'))
                        print("Save network-final-full-conv for FID: %.3f at kimg %-8.1f." %(fid_list[-1], cur_nimg//1000), flush=True)
                    else: 
                        print("No improvement for FID: %.3f at kimg %-8.1f." %(fid_list[-1], cur_nimg//1000), flush=True)
                if fid_patience_step == fid_patience:
                    fid_stop = True
                    print("Training stopped due to FID early-stopping.", flush=True)
                    cur_nimg = total_kimg * 1000

            # Record start time of the next tick.
            tick_start_time = time.time()

    # Write final results.
    # Save final only if FID-Stopping has not happend:
    if fid_stop == False:
        fid = compute_fid(Gs=Gs,minibatch_size=sched.minibatch, dataset_obj=training_set, iter_number=cur_nimg/1000, lod = 0.0, num_images=10000, printing=False)
        print("Final FID: %.3f at kimg %-8.1f." %(fid, cur_nimg//1000), flush=True)
        ### save final FID to .csv file in result_parent_dir
        csv_file = os.path.join(os.path.dirname(os.path.dirname(result_subdir)),"results_full_conv.csv")
        list_to_append = [result_subdir.split("/")[-2]+"/"+result_subdir.split("/")[-1], fid]
        with open(csv_file, 'a') as f_object:
            writer_object = writer(f_object) 
            writer_object.writerow(list_to_append)
            f_object.close()
        misc.save_pkl((G, D, Gs), os.path.join(result_subdir, 'network-final-full-conv.pkl'))
        print("Save network-final-full-conv.", flush=True)
    summary_log.close()
    open(os.path.join(result_subdir, '_training-done.txt'), 'wt').close()

def train(data_dir, results_dir, resume_run_id_dir, resume_kimgs, random_seed, resolution, num_gpus, learn_rate, batch_size, disc_repeats, total_kimg):
    return data_dir, results_dir, resume_run_id_dir, resume_kimgs, random_seed, resolution, num_gpus, learn_rate, batch_size, disc_repeats, total_kimg
#----------------------------------------------------------------------------
def execute_cmdline(argv):
    prog = argv[0]
    parser = argparse.ArgumentParser()     
    subparsers = parser.add_subparsers(dest='command')
    subparsers.required = True
    def add_command(cmd, desc, example=None):
        epilog = 'Example: %s %s' % (prog, example) if example is not None else None
        return subparsers.add_parser(cmd, description=desc, help=desc, epilog=epilog)

    p = add_command('train',                                         'Train')
    p.add_argument('data_dir',                    help='Data Base Direcotry')
    p.add_argument('results_dir',                   help='Results Directory')
    p.add_argument('resume_run_id_dir',            help='Resume run ID path')
    p.add_argument('resume_kimgs',type=int,default=4000, help='Resume number of images')
    p.add_argument('random_seed',               type=int, help='Random Seed')
    p.add_argument('resolution',                type=int,  help='Resolution')
    p.add_argument('num_gpus',     type=int, default=8, help='Number of GPUs')
    p.add_argument('learn_rate',   type=float, default=0.005, help='Learning Rates')
    p.add_argument('batch_size',   type=int, default = 256,  help='batch size')
    p.add_argument('disc_repeats', type=int, default=1,   help='Disc repeats per Gen')
    p.add_argument('total_kimg',   type=int,default=4000, help='Total k_train images')


    args = parser.parse_args(argv[1:] if len(argv) > 1 else ['-h'])
    func = globals()[args.command]
    del args.command
    return func(**vars(args))
#----------------------------------------------------------------------------
# Main entry point.
# Calls the function indicated in config.py.

if __name__ == "__main__":
    config.data_dir, config.result_dir,network_run_id, num_imgs_resume, config.random_seed, config.dataset.resolution, config.num_gpus, lr, bs, dr, tot_k = execute_cmdline(sys.argv)
    config.sched.G_lrate_base, config.sched.D_lrate_base = lr, lr
    config.sched.lod_initial_resolution = config.dataset.resolution
    config.sched.minibatch_dict = {config.dataset.resolution : bs}
    config.train.D_repeats = dr
    config.train.total_kimg = tot_k
    config.train.resume_run_id = network_run_id
    config.train.resume_kimg = num_imgs_resume

    np.random.seed(config.random_seed)
    print('Initializing TensorFlow...', flush=True)
    os.environ.update(config.env)
    tfutil.init_tf(config.tf_config)
    print('Running %s()...' % config.train['func'], flush=True)
    tfutil.call_func_by_name(**config.train)
    print('Exiting...', flush=True)

#----------------------------------------------------------------------------
