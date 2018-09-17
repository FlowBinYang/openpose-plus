#!/usr/bin/python3
import argparse
import os

import numpy as np
import tensorflow as tf
import tensorlayer as tl
import tensorrt as trt
import uff
from tensorrt.parsers import uffparser

from inference.common import measure, rename_tensor, read_imgfile
from inference.estimator2 import PoseEstimator
from models import _input_image, get_base_model_func, get_full_model_func
from idx import write_idx

tf.logging.set_verbosity(tf.logging.DEBUG)
tl.logging.set_verbosity(tl.logging.DEBUG)


def get_model_func(base_model_name):

    h, w = 368, 432
    target_size = (w, h)
    n_pos = 19

    def model_func():

        base_model = get_base_model_func(base_model_name)
        data_format = 'channels_last'
        image = _input_image(target_size[1], target_size[0], data_format, 'image')
        _, b1_list, b2_list, _ = base_model(image, n_pos, None, None, False, False, data_format=data_format)
        conf_tensor = b1_list[-1].outputs
        pafs_tensor = b2_list[-1].outputs

        with tf.variable_scope('outputs'):
            return [
                rename_tensor(conf_tensor, 'conf'),
                rename_tensor(pafs_tensor, 'paf'),
            ]

    return model_func


def volume(shape):
    v = 1
    for d in shape:
        v *= d
    return v


def infer(engine, x, batch_size):
    import pycuda.autoinit as _
    import pycuda.driver as cuda
    n = engine.get_nb_bindings()
    print('%d bindings' % n)

    mems = []  # CPU mem
    d_mems = []  # CUDA mem
    shapes = []
    for i in range(n):
        dims = engine.get_binding_dimensions(i)
        shape = dims.shape()
        cnt = volume(shape) * batch_size
        mem = cuda.pagelocked_empty(cnt, dtype=np.float32)
        d_mem = cuda.mem_alloc(cnt * mem.dtype.itemsize)
        shapes.append(shape)
        mems.append(mem)
        d_mems.append(d_mem)

    np.copyto(mems[0], x.flatten())

    stream = cuda.Stream()

    ids = list(range(n))
    inputs_ids = ids[:1]
    outputs_ids = ids[1:]

    for i in inputs_ids:
        cuda.memcpy_htod_async(d_mems[i], mems[i], stream)
    context = engine.create_execution_context()
    context.enqueue(batch_size, [int(p) for p in d_mems], stream.handle, None)
    for i in outputs_ids:
        cuda.memcpy_dtoh_async(mems[i], d_mems[i], stream)
    return [mems[i].reshape(shapes[i]) for i in outputs_ids]


def debug_tensor(t, name):
    print('%s :: %s, min: %f, mean: %f, max: %f, std: %f' % (name, t.shape, t.min(), t.mean(), t.max(), t.std()))


from cv2 import imwrite


def _normalize(t):
    t -= t.mean()
    r = t.max() - t.min()
    if r > 0:
        t /= r
    return t


def save_hwc(prefix, t):
    h, w, c = t.shape
    for i in range(c):
        name = '%s-%d.png' % (prefix, i)
        img = _normalize(t[:, :, i]) * 255.0
        # debug_tensor(img)
        imwrite(name, img)
        print('saved %s' % name)


def parse_args():
    parser = argparse.ArgumentParser(description='inference')
    parser.add_argument(
        '--path-to-npz',
        type=str,
        default=os.path.join(os.getenv('HOME'), 'Downloads/vgg450000_no_cpm.npz'),
        help='path to npz',
        required=False)
    parser.add_argument('--image', type=str, default='', help='image filename', required=True)
    parser.add_argument('--base-model', type=str, default='vgg', help='vgg | mobilenet')
    parser.add_argument('--data-format', type=str, default='channels_last', help='channels_last | channels_first.')
    parser.add_argument('--plot', type=bool, default=False, help='draw the results')
    parser.add_argument('--repeat', type=int, default=1, help='repeat the images for n times for profiling.')
    parser.add_argument('--limit', type=int, default=100, help='max number of images.')
    return parser.parse_args()


def main():
    args = parse_args()
    height, width, channel = 368, 432, 3
    x = read_imgfile(args.image, width, height)

    model_func = get_model_func('vgg')
    model_parameters = model_func()
    output_names = [p.name[:-2] for p in model_parameters]

    print('output names: %s' % ','.join(output_names))

    with tf.Session() as sess:
        sess.run(tf.global_variables_initializer())
        measure(lambda: tl.files.load_and_assign_npz_dict(args.path_to_npz, sess), 'load npz')
        frozen_graph = tf.graph_util.convert_variables_to_constants(sess, sess.graph_def, output_names)
        tf_model = tf.graph_util.remove_training_nodes(frozen_graph)
        uff_model = uff.from_tensorflow(tf_model, output_names)
        print('uff model created')

    parser = uffparser.create_uff_parser()
    parser.register_input(
        'image',
        (channel, height, width),
        0  # this value doesn't affect result in Python
    )
    for name in output_names:
        parser.register_output(name)

    G_LOGGER = trt.infer.ConsoleLogger(trt.infer.LogSeverity.INFO)
    engine = trt.utils.uff_to_trt_engine(G_LOGGER, uff_model, parser, 1, 1 << 30)
    print('engine created')

    conf, paf = infer(engine, x, 1)
    conf_f = conf[:, :, :18]
    conf_b = conf[:, :, 18:]

    write_idx('uff-conf.idx', conf)
    write_idx('uff-conf_f.idx', conf_f)
    write_idx('uff-conf_b.idx', conf_b)
    write_idx('uff-paf.idx', paf)

    debug_tensor(conf, 'conf')
    debug_tensor(conf_f, 'conf_f')
    debug_tensor(conf_b, 'conf_b')
    debug_tensor(paf, 'paf')

    save_hwc('paf', paf)
    save_hwc('conf', conf)


main()