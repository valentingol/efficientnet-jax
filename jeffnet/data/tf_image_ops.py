""" TF image operations

A collection of image operations from a variety of sources, intended for use by
RandAug / AutoAug / SimCLR policies

Original sources
  * https://github.com/tensorflow/tpu/blob/master/models/official/efficientnet/autoaugment.py
  * https://github.com/google-research/fixmatch/tree/master/imagenet/augment
  * https://github.com/tensorflow/addons/tree/v0.12.0/tensorflow_addons/image
"""
import math
import tensorflow as tf
from tensorflow_addons import image as tfi
from tensorflow_addons.image.utils import unwrap, wrap
from tensorflow_addons.utils.types import TensorLike, Number


equalize = tfi.equalize
cutout = tfi.random_cutout


def blend(image1: TensorLike, image2: TensorLike, factor: Number) -> tf.Tensor:
    """Blend `image1` and `image2` using `factor`.

    Factor can be above 0.0.  A value of 0.0 means only `image1` is used.
    A value of 1.0 means only `image2` is used.  A value between 0.0 and
    1.0 means we linearly interpolate the pixel values between the two
    images.  A value greater than 1.0 "extrapolates" the difference
    between the two pixel values, and we clip the results to values
    between 0 and 255.

    Args:
      image1: An image Tensor of shape
          `(num_rows, num_columns, num_channels)` (HWC), or
          `(num_rows, num_columns)` (HW), or
          `(num_channels, num_rows, num_columns)` (CHW).
      image2: An image Tensor of shape
          `(num_rows, num_columns, num_channels)` (HWC), or
          `(num_rows, num_columns)` (HW), or
          `(num_channels, num_rows, num_columns)`.
      factor: A floating point value or Tensor of type `tf.float32` above 0.0.

    Returns:
      A blended image Tensor of `tf.float32`.

    """
    with tf.name_scope("blend"):
        if factor == 0.0:
            return image1
        if factor == 1.0:
            return image2

        image1 = tf.cast(image1, dtype=tf.dtypes.float32)
        image2 = tf.cast(image2, dtype=tf.dtypes.float32)
        difference = image2 - image1
        scaled = factor * difference

        # Do addition in float.
        temp = image1 + scaled

        # Interpolate
        if factor > 0.0 and factor < 1.0:
            # Interpolation means we always stay within 0 and 255.
            return tf.cast(temp, tf.uint8)

        # Extrapolate:
        # We need to clip and then cast.
        return tf.image.convert_image_dtype(temp, dtype=tf.uint8, saturate=True)


def invert(image):
    """Inverts the image pixels."""
    if image.dtype == tf.uint8:
        image = 255 - image
    else:
        image = 1. - image
    return image


def solarize(image, threshold=128):
    # For each pixel in the image, select the pixel
    # if the value is less than the threshold.
    # Otherwise, subtract 255 from the pixel.
    return tf.where(image < threshold, image, invert(image))


def solarize_add(image, addition=0, threshold=128):
    # For each pixel in the image less than threshold
    # we add 'addition' amount to it and then clip the
    # pixel value to be between 0 and 255. The value
    # of 'addition' is between -128 and 128.
    addition = tf.cast(addition, image.dtype)
    added_image = image + addition
    added_image = tf.clip_by_value(
        added_image,
        tf.image.convert_image_dtype(tf.constant(0, tf.uint8), image.dtype),
        tf.image.convert_image_dtype(tf.constant(255, tf.uint8), image.dtype))
    return solarize(added_image, threshold)


def color(image, factor):
    """Equivalent of PIL Color."""
    degenerate = tf.image.grayscale_to_rgb(tf.image.rgb_to_grayscale(image))
    return blend(degenerate, image, factor)


def contrast(image, factor):
    """Equivalent of PIL Contrast."""
    degenerate = tf.image.rgb_to_grayscale(image)
    # Cast before calling tf.histogram.
    degenerate = tf.cast(degenerate, tf.int32)

    # Compute the grayscale histogram, then compute the mean pixel value,
    # and create a constant image size of that value.  Use that as the
    # blending degenerate target of the original image.
    hist = tf.histogram_fixed_width(degenerate, [0, 255], nbins=256)
    mean = tf.reduce_sum(tf.cast(hist, tf.float32)) / 256.0
    degenerate = tf.ones_like(degenerate, dtype=tf.float32) * mean
    degenerate = tf.clip_by_value(degenerate, 0.0, 255.0)
    degenerate = tf.image.grayscale_to_rgb(tf.cast(degenerate, tf.uint8))
    return blend(degenerate, image, factor)


def contrast2(image, magnitude):
    """Adjusts the `magnitude` of contrast of an `image`.
    Args:
        image: An int or float tensor of shape `[height, width, num_channels]`.
        magnitude: A 0-D float tensor or single floating point value above 0.0.
    Returns:
        A tensor with same shape and type as that of `image`.
    """
    grayed_image = tf.image.rgb_to_grayscale(image)
    grayed_image = tf.cast(grayed_image, tf.int32)
    bins = tf.constant(256, tf.int32)
    histogram = tf.math.bincount(grayed_image, minlength=bins)
    histogram = tf.cast(histogram, tf.float32)
    mean = tf.reduce_sum(tf.cast(grayed_image, tf.float32)) / tf.reduce_sum(histogram)
    mean = tf.clip_by_value(mean, 0.0, 255.0)

    mean_image = tf.ones_like(grayed_image, tf.uint8) * tf.cast(mean, tf.uint8)
    mean_image = tf.image.grayscale_to_rgb(mean_image)
    return blend(mean_image, image, magnitude)


def brightness(image, factor):
    """Equivalent of PIL Brightness."""
    degenerate = tf.zeros_like(image)
    return blend(degenerate, image, factor)


def sharpness(image, factor):
    """Implements Sharpness function from PIL using TF ops."""
    orig_image = image
    image = tf.cast(image, tf.float32)
    image_channels = image.shape[-1]
    # Make image 4D for conv operation.
    image = tf.expand_dims(image, 0)
    # SMOOTH PIL Kernel.
    kernel = tf.constant([[1, 1, 1], [1, 5, 1], [1, 1, 1]], dtype=tf.float32, shape=[3, 3, 1, 1]) / 13.
    # Tile across channel dimension.
    kernel = tf.tile(kernel, [1, 1, image_channels, 1])
    strides = [1, 1, 1, 1]
    with tf.device('/cpu:0'):
        # Some augmentation that uses depth-wise conv will cause crashing when
        # training on GPU. See (b/156242594) for details.
        degenerate = tf.nn.depthwise_conv2d(image, kernel, strides, padding='VALID')
    degenerate = tf.squeeze(tf.image.convert_image_dtype(degenerate, dtype=tf.uint8, saturate=True), [0])

    # For the borders of the resulting image, fill in the values of the
    # original image.
    mask = tf.ones_like(degenerate)
    padded_mask = tf.pad(mask, [[1, 1], [1, 1], [0, 0]])
    padded_degenerate = tf.pad(degenerate, [[1, 1], [1, 1], [0, 0]])
    result = tf.where(tf.equal(padded_mask, 1), padded_degenerate, orig_image)

    # Blend the final result.
    return blend(result, orig_image, factor)


def posterize(image, num_bits):
    """Equivalent of PIL Posterize."""
    shift = 8 - num_bits
    return tf.bitwise.left_shift(tf.bitwise.right_shift(image, shift), shift)


def posterize2(image, num_bits):
    """Reduces the number of bits used to represent an `image`
    for each color channel.
    Args:
        image: An int or float tensor of shape `[height, width, num_channels]`.
        num_bits: A 0-D int tensor or integer value representing number of bits.
    Returns:
        A tensor with same shape and type as that of `image`.
    """
    num_bits = tf.cast(num_bits, tf.int32)
    mask = tf.cast(2 ** (8 - num_bits) - 1, tf.uint8)
    mask = tf.bitwise.invert(mask)

    posterized_image = tf.bitwise.bitwise_and(image, mask)
    return posterized_image


def rotate(image, degrees, fill_value, interpolation='BILINEAR'):
    """Rotates the image by degrees either clockwise or counterclockwise.

    Args:
        image: An image Tensor of type uint8.
        degrees: Float, a scalar angle in degrees to rotate all images by. If degrees is positive the image
            will be rotated clockwise otherwise it will be rotated counterclockwise.
        fill_value: A one or three value 1D tensor to fill empty pixels caused by the rotate operation.
        interpolation: Interpolation method
    Returns:
        The rotated version of image.
    """
    # Convert from degrees to radians.
    degrees_to_radians = math.pi / 180.0
    radians = degrees * degrees_to_radians

    # In practice, we should randomize the rotation degrees by flipping
    # it negatively half the time, but that's done on 'degrees' outside
    # of the function.
    image = tfi.rotate(wrap(image), radians, interpolation=interpolation)
    return unwrap(image, fill_value)


def translate_x(image, pixels, fill_value):
    """Equivalent of PIL Translate in X dimension."""
    image = tfi.translate(wrap(image), [-pixels, 0])
    return unwrap(image, fill_value)


def translate_y(image, pixels, fill_value):
    """Equivalent of PIL Translate in Y dimension."""
    image = tfi.translate(wrap(image), [0, -pixels])
    return unwrap(image, fill_value)


def shear_x(image, level, fill_value):
    """Equivalent of PIL Shearing in X dimension."""
    # Shear parallel to x axis is a projective transform
    # with a matrix form of:
    # [1  level
    #  0  1].
    image = tfi.transform(wrap(image), [1., level, 0., 0., 1., 0., 0., 0.])
    return unwrap(image, fill_value)


def shear_y(image, level, fill_vaue):
    """Equivalent of PIL Shearing in Y dimension."""
    # Shear parallel to y axis is a projective transform
    # with a matrix form of:
    # [1  0
    #  level  1].
    image = tfi.transform(wrap(image), [1., 0., 0., level, 1., 0., 0., 0.])
    return unwrap(image, fill_vaue)


def autocontrast(image):
    """Normalizes `image` contrast by remapping the `image` histogram such
    that the brightest pixel becomes 1.0 (float) / 255 (unsigned int) and
    darkest pixel becomes 0.
    Args:
        image: An int or float tensor of shape `[height, width, num_channels]`.
    Returns:
        A tensor with same shape and type as that of `image`.
    """
    orig_dtype = image.dtype
    image = tf.image.convert_image_dtype(image, tf.float32)

    min_val, max_val = tf.reduce_min(image, axis=[0, 1]), tf.reduce_max(image, axis=[0, 1])

    norm_image = (image - min_val) / (max_val - min_val)
    norm_image = tf.image.convert_image_dtype(norm_image, orig_dtype, saturate=True)
    return norm_image


@tf.function
def autocontrast2(image):
    """Implements Autocontrast function from PIL using TF ops.

    Args:
      image: A 3D uint8 tensor.

    Returns:
      The image after it has had autocontrast applied to it and will be of type
      uint8.
    """

    def scale_channel(image_ch):
        """Scale the 2D image using the autocontrast rule."""
        # A possibly cheaper version can be done using cumsum/unique_with_counts
        # over the histogram values, rather than iterating over the entire image.
        # to compute mins and maxes.
        lo = tf.to_float(tf.reduce_min(image_ch))
        hi = tf.to_float(tf.reduce_max(image_ch))

        # Scale the image, making the lowest value 0 and the highest value 255.
        def scale_values(im):
            scale = 255.0 / (hi - lo)
            offset = -lo * scale
            im = tf.to_float(im) * scale + offset
            im = tf.clip_by_value(im, 0.0, 255.0)
            return tf.cast(im, tf.uint8)

        result = tf.cond(hi > lo, lambda: scale_values(image_ch), lambda: image_ch)
        return result

    # Scales each channel independently and then stacks the result.
    image = tf.stack([scale_channel(image[:, :, i]) for i in range(tf.shape(image)[2])], 2)
    return image