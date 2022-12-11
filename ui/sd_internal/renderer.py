import queue
import time
import json
import os
import base64
import re
import traceback
import logging

from sd_internal import device_manager
from sd_internal import TaskData, Response, Image as ResponseImage, UserInitiatedStop

from modules import model_loader, image_generator, image_utils, filters as image_filters
from modules.types import Context, GenerateImageRequest

log = logging.getLogger()

context = Context() # thread-local
'''
runtime data (bound locally to this thread), for e.g. device, references to loaded models, optimization flags etc
'''

filename_regex = re.compile('[^a-zA-Z0-9]')

def init(device):
    '''
    Initializes the fields that will be bound to this runtime's context, and sets the current torch device
    '''
    context.stop_processing = False
    context.temp_images = {}
    context.partial_x_samples = None

    device_manager.device_init(context, device)

def make_images(req: GenerateImageRequest, task_data: TaskData, data_queue: queue.Queue, task_temp_images: list, step_callback):
    try:
        return _make_images_internal(req, task_data, data_queue, task_temp_images, step_callback)
    except Exception as e:
        log.error(traceback.format_exc())

        data_queue.put(json.dumps({
            "status": 'failed',
            "detail": str(e)
        }))
        raise e

def _make_images_internal(req: GenerateImageRequest, task_data: TaskData, data_queue: queue.Queue, task_temp_images: list, step_callback):
    images, user_stopped = generate_images(req, data_queue, task_temp_images, step_callback, task_data.stream_image_progress)
    images = apply_filters(task_data, images, user_stopped, task_data.show_only_filtered_image)

    if task_data.save_to_disk_path is not None:
        out_path = os.path.join(task_data.save_to_disk_path, filename_regex.sub('_', task_data.session_id))
        save_images(images, out_path, metadata=req.to_metadata(), show_only_filtered_image=task_data.show_only_filtered_image)

    res = Response(req, task_data, images=construct_response(images))
    res = res.json()
    data_queue.put(json.dumps(res))
    log.info('Task completed')

    return res

def generate_images(req: GenerateImageRequest, data_queue: queue.Queue, task_temp_images: list, step_callback, stream_image_progress: bool):
    log.info(req.to_metadata())
    context.temp_images.clear()

    image_generator.on_image_step = make_step_callback(req, data_queue, task_temp_images, step_callback, stream_image_progress)

    try:
        images = image_generator.make_images(context=context, req=req)
        user_stopped = False
    except UserInitiatedStop:
        images = []
        user_stopped = True
        if context.partial_x_samples is not None:
            images = image_utils.latent_samples_to_images(context, context.partial_x_samples)
            context.partial_x_samples = None
    finally:
        model_loader.gc(context)

    images = [(image, req.seed + i, False) for i, image in enumerate(images)]

    return images, user_stopped

def apply_filters(task_data: TaskData, images: list, user_stopped, show_only_filtered_image):
    if user_stopped or (task_data.use_face_correction is None and task_data.use_upscale is None):
        return images

    filters = []
    if 'gfpgan' in task_data.use_face_correction.lower(): filters.append(image_filters.apply_gfpgan)
    if 'realesrgan' in task_data.use_face_correction.lower(): filters.append(image_filters.apply_realesrgan)

    filtered_images = []
    for img, seed, _ in images:
        for filter_fn in filters:
            img = filter_fn(context, img)

        filtered_images.append((img, seed, True))

    if not show_only_filtered_image:
        filtered_images = images + filtered_images

    return filtered_images

def save_images(images: list, save_to_disk_path, metadata: dict, show_only_filtered_image):
    if save_to_disk_path is None:
        return

    def get_image_id(i):
        img_id = base64.b64encode(int(time.time()+i).to_bytes(8, 'big')).decode() # Generate unique ID based on time.
        img_id = img_id.translate({43:None, 47:None, 61:None})[-8:] # Remove + / = and keep last 8 chars.
        return img_id

    def get_image_basepath(i):
        os.makedirs(save_to_disk_path, exist_ok=True)
        prompt_flattened = filename_regex.sub('_', metadata['prompt'])[:50]
        return os.path.join(save_to_disk_path, f"{prompt_flattened}_{get_image_id(i)}")

    for i, img_data in enumerate(images):
        img, seed, filtered = img_data
        img_path = get_image_basepath(i)

        if not filtered or show_only_filtered_image:
            img_metadata_path = img_path + '.txt'
            m = metadata.copy()
            m['seed'] = seed
            with open(img_metadata_path, 'w', encoding='utf-8') as f:
                f.write(m)

        img_path += '_filtered' if filtered else ''
        img_path += '.' + metadata['output_format']
        img.save(img_path, quality=metadata['output_quality'])

def construct_response(task_data: TaskData, images: list):
    return [
        ResponseImage(
            data=image_utils.img_to_base64_str(img, task_data.output_format, task_data.output_quality),
            seed=seed
        ) for img, seed, _ in images
    ]

def make_step_callback(req: GenerateImageRequest, task_data: TaskData, data_queue: queue.Queue, task_temp_images: list, step_callback, stream_image_progress: bool):
    n_steps = req.num_inference_steps if req.init_image is None else int(req.num_inference_steps * req.prompt_strength)
    last_callback_time = -1

    def update_temp_img(x_samples, task_temp_images: list):
        partial_images = []
        for i in range(req.num_outputs):
            img = image_utils.latent_to_img(context, x_samples[i].unsqueeze(0))
            buf = image_utils.img_to_buffer(img, output_format='JPEG')

            del img

            context.temp_images[f"{task_data.request_id}/{i}"] = buf
            task_temp_images[i] = buf
            partial_images.append({'path': f"/image/tmp/{task_data.request_id}/{i}"})
        return partial_images

    def on_image_step(x_samples, i):
        nonlocal last_callback_time

        context.partial_x_samples = x_samples
        step_time = time.time() - last_callback_time if last_callback_time != -1 else -1
        last_callback_time = time.time()

        progress = {"step": i, "step_time": step_time, "total_steps": n_steps}

        if stream_image_progress and i % 5 == 0:
            progress['output'] = update_temp_img(x_samples, task_temp_images)

        data_queue.put(json.dumps(progress))

        step_callback()

        if context.stop_processing:
            raise UserInitiatedStop("User requested that we stop processing")

    return on_image_step