import time

import numpy as np
from scipy.ndimage.interpolation import rotate
import skimage
import skimage.transform
from skimage.transform._warps_cy import _warp_fast

import util


STD = np.array([70.53946096, 51.71475228, 43.03428563], dtype=np.float32)
MEAN = np.array([108.64628601, 75.86886597, 54.34005737], dtype=np.float32)

# computed with make_pca.py
U = np.array([[-0.56543481, 0.71983482, 0.40240142],
              [-0.5989477, -0.02304967, -0.80036049],
              [-0.56694071, -0.6935729, 0.44423429]] ,dtype=np.float32)
EV = np.array([1.65513492, 0.48450358, 0.1565086], dtype=np.float32)


default_augmentation_params = {
    'zoom_range': (1 / 1.1, 1.1),
    'rotation_range': (0, 360),
    'shear_range': (0, 0),
    'translation_range': (-40, 40),
    'do_flip': True,
    'allow_stretch': True,
}


no_augmentation_params = {
    'zoom_range': (1.0, 1.0),
    'rotation_range': (0, 0),
    'shear_range': (0, 0),
    'translation_range': (0, 0),
    'do_flip': False,
    'allow_stretch': False,
}

# timings for in 512px out 448px: order=0: 19.8ms, order=1: 26ms
def fast_warp(img, tf, output_shape=(W, H), mode='constant', order=0):
    """
    This wrapper function is faster than skimage.transform.warp
    """
    m = tf.params # tf._matrix is
    #return skimage.transform._warps_cy._warp_fast(img, m, output_shape=output_shape, mode=mode, order=order)
    t_img = np.zeros((img.shape[0],) + output_shape, img.dtype)
    for i in range(t_img.shape[0]):
        #t_img[i] = skimage.transform.warp(img[i], m, output_shape=output_shape, 
        #                                  mode=mode, order=order)
        t_img[i] = _warp_fast(img[i], m, output_shape=output_shape, 
                              mode=mode, order=order)
    return t_img


def build_centering_transform(image_shape, target_shape=(W, H)):
    rows, cols = image_shape
    trows, tcols = target_shape
    shift_x = (cols - tcols) / 2.0
    shift_y = (rows - trows) / 2.0
    return skimage.transform.SimilarityTransform(translation=(shift_x, shift_y))


def build_rescale_transform_slow(downscale_factor, image_shape, target_shape):
    """
    This mimics the skimage.transform.resize function.
    The resulting image is centered.
    """
    rows, cols = image_shape
    trows, tcols = target_shape
    col_scale = row_scale = downscale_factor
    src_corners = np.array([[1, 1], [1, rows], [cols, rows]]) - 1
    dst_corners = np.zeros(src_corners.shape, dtype=np.double)
    # take into account that 0th pixel is at position (0.5, 0.5)
    dst_corners[:, 0] = col_scale * (src_corners[:, 0] + 0.5) - 0.5
    dst_corners[:, 1] = row_scale * (src_corners[:, 1] + 0.5) - 0.5

    tform_ds = skimage.transform.AffineTransform()
    tform_ds.estimate(src_corners, dst_corners)

    # centering    
    shift_x = cols / (2.0 * downscale_factor) - tcols / 2.0
    shift_y = rows / (2.0 * downscale_factor) - trows / 2.0
    tform_shift_ds = skimage.transform.SimilarityTransform(translation=(shift_x, shift_y))
    return tform_shift_ds + tform_ds


def build_rescale_transform_fast(downscale_factor, image_shape, target_shape):
    """
    estimating the correct rescaling transform is slow, so just use the
    downscale_factor to define a transform directly. This probably isn't 
    100% correct, but it shouldn't matter much in practice.
    """
    rows, cols = image_shape
    trows, tcols = target_shape
    tform_ds = skimage.transform.AffineTransform(scale=(downscale_factor, downscale_factor))
    
    # centering    
    shift_x = cols / (2.0 * downscale_factor) - tcols / 2.0
    shift_y = rows / (2.0 * downscale_factor) - trows / 2.0
    tform_shift_ds = skimage.transform.SimilarityTransform(translation=(shift_x, shift_y))
    return tform_shift_ds + tform_ds

build_rescale_transform = build_rescale_transform_fast


def build_center_uncenter_transforms(image_shape):
    """
    These are used to ensure that zooming and rotation happens around the center of the image.
    Use these transforms to center and uncenter the image around such a transform.
    """
    center_shift = np.array([image_shape[1], image_shape[0]]) / 2.0 - 0.5 # need to swap rows and cols here apparently! confusing!
    tform_uncenter = skimage.transform.SimilarityTransform(translation=-center_shift)
    tform_center = skimage.transform.SimilarityTransform(translation=center_shift)
    return tform_center, tform_uncenter

def build_augmentation_transform(zoom=(1.0, 1.0), rotation=0, shear=0, translation=(0, 0), flip=False): 
    if flip:
        shear += 180
        rotation += 180
        # shear by 180 degrees is equivalent to rotation by 180 degrees + flip.
        # So after that we rotate it another 180 degrees to get just the flip.

    tform_augment = skimage.transform.AffineTransform(scale=(1/zoom[0], 1/zoom[1]), rotation=np.deg2rad(rotation), shear=np.deg2rad(shear), translation=translation)
    return tform_augment

def random_perturbation_transform(zoom_range, rotation_range, shear_range, translation_range, do_flip=True, allow_stretch=False, rng=np.random):
    shift_x = rng.uniform(*translation_range)
    shift_y = rng.uniform(*translation_range)
    translation = (shift_x, shift_y)

    rotation = rng.uniform(*rotation_range)
    shear = rng.uniform(*shear_range)

    if do_flip:
        flip = (rng.randint(2) > 0) # flip half of the time
    else:
        flip = False

    # random zoom
    log_zoom_range = [np.log(z) for z in zoom_range]
    if isinstance(allow_stretch, float):
        log_stretch_range = [-np.log(allow_stretch), np.log(allow_stretch)]
        zoom = np.exp(rng.uniform(*log_zoom_range))
        stretch = np.exp(rng.uniform(*log_stretch_range))
        zoom_x = zoom * stretch
        zoom_y = zoom / stretch
    elif allow_stretch is True: # avoid bugs, f.e. when it is an integer
        zoom_x = np.exp(rng.uniform(*log_zoom_range))
        zoom_y = np.exp(rng.uniform(*log_zoom_range))
    else:
        zoom_x = zoom_y = np.exp(rng.uniform(*log_zoom_range))
    # the range should be multiplicatively symmetric, so [1/1.1, 1.1] instead of [0.9, 1.1] makes more sense.

    return build_augmentation_transform((zoom_x, zoom_y), rotation, shear, translation, flip)

def perturb(img, augmentation_params=default_augmentation_params, 
            target_shape=(W, H), rng=np.random):
    # # DEBUG: draw a border to see where the image ends up
    # img[0, :] = 0.5
    # img[-1, :] = 0.5
    # img[:, 0] = 0.5
    # img[:, -1] = 0.5
    shape = img.shape[1:]
    tform_centering = build_centering_transform(shape, target_shape)
    tform_center, tform_uncenter = build_center_uncenter_transforms(shape)
    tform_augment = random_perturbation_transform(rng=rng, **augmentation_params)
    tform_augment = tform_uncenter + tform_augment + tform_center # shift to center, augment, shift back (for the rotation/shearing)
    return fast_warp(img, tform_centering + tform_augment, 
                     output_shape=target_shape, 
                     mode='constant')


# for test-time augmentation
def perturb_fixed(img, tform_augment, target_shape=(50, 50)):
    shape = img.shape[1:]
    tform_centering = build_centering_transform(shape, target_shape)
    tform_center, tform_uncenter = build_center_uncenter_transforms(shape)
    tform_augment = tform_uncenter + tform_augment + tform_center # shift to center, augment, shift back (for the rotation/shearing)
    return fast_warp(img, tform_centering + tform_augment,
                     output_shape=target_shape, mode='constant')


def load_perturbed(fname):
    img = util.load_image_uint_one(fname).astype(np.float32)
    return perturb(img)

def augment_color(img, sigma=0.1, color_vec=None):

    if color_vec is None:
        color_vec = np.random.normal(0.0, sigma, 3)
    
    alpha = color_vec.astype(np.float32) * EV
    noise = np.dot(U, alpha.T)
    return img + noise[:, np.newaxis, np.newaxis]


def load(fname, *args, **kwargs):
    #tin = time.time()
    w = kwargs['w']
    h = kwargs['h']
    img = util.load_image(fname)
    if kwargs.get('deterministic') is True:
        #img = crop(img, w=w, h=h)
        img = perturb(img, augmentation_params=no_augmentation_params,
                      target_shape=(w, h))
    elif kwargs.get('rotate') is True:
        aug_params = kwargs.get('aug_params', default_augmentation_params)
        if kwargs.get('transform') is None:
            img = perturb(img, augmentation_params=aug_params,
                          target_shape=(w, h))
        else:
            img = perturb_fixed(img, tform_augment=kwargs['transform'], 
                            target_shape=(w, h))
    else:
        img = crop_random(img, w=w, h=h)

    #t2 = time.time()
    #print('load crop took {}'.format(t2 - tin))
    np.subtract(img, kwargs['mean'][:, np.newaxis, np.newaxis], out=img)
    np.divide(img, kwargs['std'][:, np.newaxis, np.newaxis], out=img)

    if not kwargs.get('deterministic') and kwargs.get('color') is True:
        img = augment_color(img, sigma=kwargs.get('sigma', SIGMA_COLOR),
                            color_vec=kwargs.get('color_vec', None))

    #np.divide(img, 128.0, out=img)
    #print('normalize took {}'.format(time.time() - t2))
    return img
