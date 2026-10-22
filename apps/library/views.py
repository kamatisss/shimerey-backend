import time
import traceback
import os
import logging
from io import BytesIO

import numpy as np
import cv2
from PIL import Image, ImageFilter, ImageOps, ImageEnhance
from django.http import JsonResponse
from rest_framework.decorators import api_view, permission_classes, throttle_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.throttling import UserRateThrottle
from django.core.files.storage import default_storage

logger = logging.getLogger(__name__)

# ───────────────────────────────────────────────────────────────
# SEGMENTATION
# ───────────────────────────────────────────────────────────────
def detect_sky(img_rgb, hsv, h=512, w=512):
    upper = np.zeros((h, w), dtype=bool)
    upper[:int(h * 0.45), :] = True

    blue = (hsv[:,:,0] >= 85) & (hsv[:,:,0] <= 140) & (hsv[:,:,1] > 20) & (hsv[:,:,2] > 90)
    white = (hsv[:,:,1] < 50) & (hsv[:,:,2] > 170)
    overcast = (hsv[:,:,1] < 35) & (hsv[:,:,2] > 130)

    sky = ((blue | white | overcast) & upper).astype(np.uint8) * 255

    k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (15, 15))
    sky = cv2.morphologyEx(sky, cv2.MORPH_CLOSE, k)
    sky = cv2.morphologyEx(sky, cv2.MORPH_OPEN, k)

    for x in range(w):
        col = sky[:, x]
        pts = np.where(col > 0)[0]
        if len(pts) > 0 and pts[0] < int(h * 0.15):
            sky[:pts[-1], x] = 255

    return sky

def detect_ground(img_rgb, hsv, sky_mask, h=512, w=512):
    green = (hsv[:,:,0] >= 25) & (hsv[:,:,0] <= 95) & (hsv[:,:,1] > 15) & (hsv[:,:,2] > 25)
    brown = (hsv[:,:,0] >= 8) & (hsv[:,:,0] <= 30) & (hsv[:,:,1] > 25) & (hsv[:,:,2] > 35)
    gray = (hsv[:,:,1] < 30) & (hsv[:,:,2] > 50) & (hsv[:,:,2] < 210)

    weight = np.zeros((h, w), dtype=np.float32)
    for y in range(h):
        weight[y, :] = max(0, (y / h - 0.15)) / 0.85

    score = (green | brown | gray).astype(np.float32) * weight
    score[sky_mask > 0] = 0

    gnd = (score > 0.2).astype(np.uint8) * 255
    k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (11, 11))
    gnd = cv2.morphologyEx(gnd, cv2.MORPH_CLOSE, k)
    gnd = cv2.morphologyEx(gnd, cv2.MORPH_OPEN, k)
    gnd = cv2.dilate(gnd, np.ones((5, 5), np.uint8), iterations=2)
    gnd = cv2.erode(gnd, np.ones((5, 5), np.uint8), iterations=2)
    return gnd

def detect_objects(sky_mask, ground_mask, h=512, w=512):
    obj = np.ones((h, w), dtype=np.uint8) * 255
    obj[sky_mask > 0] = 0
    obj[ground_mask > 0] = 0
    k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))
    obj = cv2.morphologyEx(obj, cv2.MORPH_OPEN, k)
    contours, _ = cv2.findContours(obj, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    for cnt in contours:
        if cv2.contourArea(cnt) < 150:
            cv2.drawContours(obj, [cnt], -1, 0, -1)
    return obj


# ───────────────────────────────────────────────────────────────
# OBJECT CLASSIFICATION & DETECTION
# ───────────────────────────────────────────────────────────────
# Model type mapping based on heuristics
OBJECT_CLASSES = {
    'large_tree':    ['coconut_palm', 'travelers_palm', 'frangipani'],
    'medium_tree':   ['banana', 'bird_of_paradise', 'frangipani'],
    'tall_shrub':    ['bamboo', 'bougainvillea'],
    'small_shrub':   ['hibiscus', 'duranta', 'santan'],
    'grass_cluster': ['duranta', 'santan'],
    'structure':     ['trellis_arch', 'garden_bench'],
}

def classify_and_locate_objects(object_mask, img_rgb, hsv, raw_depth, h=512, w=512):
    """Find individual object blobs, classify them, and compute 3D placement data."""
    detected = []

    contours, _ = cv2.findContours(
        object_mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE
    )

    for cnt in contours:
        area = cv2.contourArea(cnt)
        if area < 300:  # skip tiny blobs
            continue

        x, y, bw, bh = cv2.boundingRect(cnt)
        aspect = bh / max(bw, 1)
        center_x = x + bw / 2
        center_y = y + bh / 2
        bottom_y = y + bh  # base of object on ground

        # ── Classify by shape + size + color ──
        relative_height = bh / h
        relative_width = bw / w
        relative_area = area / (h * w)

        # Sample average color in the blob
        blob_mask = np.zeros((h, w), dtype=np.uint8)
        cv2.drawContours(blob_mask, [cnt], -1, 255, -1)
        mean_hsv = cv2.mean(hsv, mask=blob_mask)[:3]
        mean_green = mean_hsv[0] >= 25 and mean_hsv[0] <= 95 and mean_hsv[1] > 30

        # Average depth in blob for distance estimation
        blob_depth_vals = raw_depth[blob_mask > 0]
        avg_depth = float(np.mean(blob_depth_vals)) if len(blob_depth_vals) > 0 else 0.5

        # Classification heuristics
        if relative_height > 0.35 and aspect > 1.5:
            obj_class = 'large_tree'
        elif relative_height > 0.2 and aspect > 1.2:
            obj_class = 'medium_tree'
        elif relative_height > 0.12 and aspect > 0.8:
            obj_class = 'tall_shrub'
        elif relative_area > 0.005 and mean_green:
            obj_class = 'small_shrub'
        elif mean_green:
            obj_class = 'grass_cluster'
        else:
            obj_class = 'structure'

        # Pick a model type from the class
        import random
        model_options = OBJECT_CLASSES.get(obj_class, ['duranta'])
        model_type = random.choice(model_options)

        # ── Compute 3D placement ──
        # Convert image coords to normalized [-1, 1] range
        norm_x = (center_x / w) * 2 - 1    # -1 left, +1 right
        norm_z = (bottom_y / h) * 2 - 1     # -1 top, +1 bottom (close to camera)

        # Scale based on blob size + depth
        # Larger blobs at closer depth = bigger scale
        scale_factor = max(0.3, min(2.0,
            relative_height * 4.0 * (0.5 + avg_depth * 0.5)
        ))

        detected.append({
            'class': obj_class,
            'model_type': model_type,
            'image_bbox': {'x': int(x), 'y': int(y), 'w': int(bw), 'h': int(bh)},
            'position': {
                'norm_x': round(float(norm_x), 3),
                'norm_z': round(float(norm_z), 3),
                'depth': round(float(avg_depth), 3),
            },
            'scale': round(float(scale_factor), 2),
            'area_percent': round(float(relative_area * 100), 1),
        })

    # Sort by area (largest first)
    detected.sort(key=lambda d: d['area_percent'], reverse=True)

    # Cap at 25 objects max for performance
    detected = detected[:25]

    logger.info(f"Detected {len(detected)} objects: " +
                ", ".join(f"{d['class']}({d['model_type']})" for d in detected[:8]))

    return detected


# ───────────────────────────────────────────────────────────────
# DEPTH MAP GENERATION (flat ground for terrain)
# ───────────────────────────────────────────────────────────────
def generate_flat_terrain_depth(sky_mask, ground_mask, object_mask, h=512, w=512):
    """Generate a nearly-flat depth map for terrain rendering.
    Objects are handled by 3D models, NOT by terrain extrusion."""

    depth = np.zeros((h, w), dtype=np.float32)

    # Ground: very gentle perspective slope (almost flat)
    y_norm = np.arange(h, dtype=np.float32).reshape(-1, 1) / h
    base_height = 0.1 + 0.05 * np.sin(y_norm * np.pi * 0.5)

    # Apply base height everywhere (no cliffs for sky!)
    depth = np.broadcast_to(base_height, (h, w)).copy()

    # Objects: give a VERY SLIGHT bump
    obj_region = object_mask > 0
    if np.any(obj_region):
        depth[obj_region] += 0.015

    # Smooth everything
    depth = cv2.GaussianBlur(depth, (21, 21), 0)
    depth = cv2.bilateralFilter(depth, 9, 0.05, 40)

    # Do NOT normalize to 0-1, otherwise a tiny bump (0.02) becomes a huge mountain
    # depth is already bounded [0, 1] by construction
    depth = np.clip(depth, 0.0, 1.0)

    return depth


def generate_normal_map(depth, h=512, w=512):
    sx = cv2.Sobel(depth, cv2.CV_32F, 1, 0, ksize=5)
    sy = cv2.Sobel(depth, cv2.CV_32F, 0, 1, ksize=5)
    nm = np.zeros((h, w, 3), dtype=np.float32)
    nm[:,:,0] = -sx
    nm[:,:,1] = -sy
    nm[:,:,2] = 1.0
    norms = np.linalg.norm(nm, axis=2, keepdims=True) + 1e-6
    nm = nm / norms
    return (nm * 127.5 + 127.5).astype(np.uint8)


def cv2_heuristic_depth(gray_array):
    h = gray_array.shape[0]
    vert = np.linspace(0.3, 1.0, h).reshape(-1, 1).astype(np.float32)
    gray_u8 = (gray_array * 255).astype(np.uint8)
    edges = cv2.Canny(gray_u8, 30, 100).astype(np.float32) / 255.0
    edges = cv2.GaussianBlur(edges, (11, 11), 0)
    depth = vert * 0.7 + edges * 0.3
    depth = cv2.GaussianBlur(depth, (15, 15), 0)
    depth = (depth - depth.min()) / (depth.max() - depth.min() + 1e-8)
    return depth


def _to_pil_gray(arr):
    return Image.fromarray(np.clip(arr * 255, 0, 255).astype(np.uint8))


# ───────────────────────────────────────────────────────────────
# MAIN ENDPOINT
# ───────────────────────────────────────────────────────────────
class DepthMapThrottle(UserRateThrottle):
    """Photo-to-terrain runs a heavy local model, so it's limited per account (settings: 'depth_maps')."""
    scope = 'depth_maps'


@api_view(['POST'])
@permission_classes([IsAuthenticated])
@throttle_classes([DepthMapThrottle])
def generate_depth(request):
    """
    Hybrid 3D pipeline:
    1. Segment sky/ground/objects
    2. Classify & locate individual objects
    3. Generate FLAT terrain depth (no column extrusions)
    4. Return terrain textures + detected_objects for 3D model placement
    """
    if request.method != 'POST':
        return JsonResponse({'error': 'POST only'}, status=405)
    if 'image' not in request.FILES:
        return JsonResponse({'error': 'No image provided'}, status=400)

    try:
        image_file = request.FILES['image']
        logger.info(f"Processing: {image_file.name} ({image_file.size} bytes)")

        if not image_file.content_type.startswith('image/'):
            return JsonResponse({'error': 'Must be an image'}, status=400)

        # ═══ LOAD ═══
        img = Image.open(image_file).convert('RGB')
        original_size = img.size
        
        # Use BILINEAR instead of LANCZOS for significantly faster resizing,
        # especially important for large phone camera photos
        img = img.resize((512, 512), Image.Resampling.BILINEAR)
        
        img_uint8 = np.array(img, dtype=np.uint8)
        img_np = img_uint8.astype(np.float32) / 255.0
        gray = np.array(img.convert('L'), dtype=np.float32) / 255.0
        hsv = cv2.cvtColor(img_uint8, cv2.COLOR_RGB2HSV)

        # ═══ SEGMENT ═══
        sky_mask = detect_sky(img_uint8, hsv)
        ground_mask = detect_ground(img_uint8, hsv, sky_mask)
        object_mask = detect_objects(sky_mask, ground_mask)

        sky_pct = np.sum(sky_mask > 0) / (512*512) * 100
        gnd_pct = np.sum(ground_mask > 0) / (512*512) * 100
        obj_pct = np.sum(object_mask > 0) / (512*512) * 100
        logger.info(f"Segments: sky={sky_pct:.0f}% ground={gnd_pct:.0f}% objects={obj_pct:.0f}%")

        # ═══ RAW DEPTH (for object distance estimation) ═══
        # A light OpenCV estimate. (The MiDaS/torch model used to do this; it only nudged object
        # sizes, the terrain is identical without it, and torch is too large for small hosting plans.)
        raw_depth = cv2_heuristic_depth(gray)

        # ═══ DETECT & CLASSIFY OBJECTS ═══
        detected_objects = classify_and_locate_objects(
            object_mask, img_uint8, hsv, raw_depth
        )

        # ═══ FLAT TERRAIN DEPTH ═══
        terrain_depth = generate_flat_terrain_depth(
            sky_mask, ground_mask, object_mask
        )

        # ═══ NORMAL MAP ═══
        normal_map = generate_normal_map(terrain_depth)

        # ═══ SAVE MAPS ═══
        results = {}
        base = "".join(c for c in image_file.name.split('.')[0]
                      if c.isalnum() or c in ('-', '_'))[:50]
        ts = int(time.time())

        for name, pil_img in [
            ('depth', _to_pil_gray(terrain_depth)),
            ('normal', Image.fromarray(normal_map)),
            ('ground_mask', _to_pil_gray((ground_mask > 0).astype(np.float32))),
            ('object_mask', _to_pil_gray((object_mask > 0).astype(np.float32))),
        ]:
            buf = BytesIO()
            # optimize=True is extremely slow (uses zlib multi-pass).
            # compress_level=1 is the fastest compression.
            pil_img.save(buf, format='PNG', compress_level=1)
            buf.seek(0)
            path = default_storage.save(f'depth_maps/{name}_{base}_{ts}.png', buf)
            results[f'{name}_url'] = f'/media/{path}'

        return JsonResponse({
            'message': 'Success',
            'depth_map_url': results.get('depth_url'),
            'normal_map_url': results.get('normal_url'),
            'rock_mask_url': results.get('ground_mask_url'),
            'grass_mask_url': results.get('object_mask_url'),
            'ground_mask_url': results.get('ground_mask_url'),
            'object_mask_url': results.get('object_mask_url'),
            'original_size': original_size,
            'detected_objects': detected_objects,
            'segmentation': {
                'sky_percent': round(sky_pct, 1),
                'ground_percent': round(gnd_pct, 1),
                'object_percent': round(obj_pct, 1),
            },
        })

    except Exception as e:
        logger.exception("Error in generate_depth:")
        return JsonResponse({
            'error': str(e), 'error_type': type(e).__name__,
            'depth_map_url': None, 'normal_map_url': None,
            'rock_mask_url': None, 'grass_mask_url': None,
            'detected_objects': [],
        }, status=500)