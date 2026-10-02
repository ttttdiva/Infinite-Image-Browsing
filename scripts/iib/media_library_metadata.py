"""画像ライブラリ用の読み取り専用メタデータ正規化。元ファイルは変更しない。"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from PIL import Image

from scripts.iib.parsers.comfyui import ComfyUIParser
from scripts.iib.parsers.sd_webui import SdWebUIParser
from scripts.iib.tool import parse_prompt

PARSER_REVISION = 1
IMAGE_EXTENSIONS = {'.png', '.jpg', '.jpeg', '.webp', '.gif', '.bmp', '.avif', '.tif', '.tiff'}
VIDEO_EXTENSIONS = {'.mp4', '.webm', '.mov', '.mkv', '.avi', '.m4v'}


def record(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def text(value: Any) -> str:
    return value if isinstance(value, str) else ''


def json_object(value: Any) -> dict:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and len(value) <= 8 * 1024 * 1024:
        try:
            return record(json.loads(value))
        except (ValueError, TypeError):
            pass
    return {}


def _caption(value: Any) -> dict:
    return record(record(value).get('caption'))


def _char_captions(value: Any) -> list[dict]:
    values = _caption(value).get('char_captions')
    return [record(x) for x in values] if isinstance(values, list) else []


def _centers(values: Any) -> list[dict]:
    result = []
    if isinstance(values, list):
        for item in values[:16]:
            row = record(item)
            x, y = row.get('x'), row.get('y')
            if isinstance(x, (int, float)) and isinstance(y, (int, float)):
                if 0 <= x <= 1 and 0 <= y <= 1:
                    result.append({'x': x, 'y': y})
    return result


def _prompt_parts(raw: str) -> tuple[str, str]:
    before, sep, after = raw.partition('\nNegative prompt:')
    if sep:
        return before.strip(), after.split('\nSteps:', 1)[0].strip()
    return raw.split('\nSteps:', 1)[0].strip(), ''


def normalize_image(image: Image.Image, path: str = '') -> dict:
    """共通promptと各キャラクターのpromptを混ぜず、実記録値だけを返す。"""
    info = image.info
    result = {
        'width': image.width, 'height': image.height, 'source': '', 'model': '',
        'positive': '', 'negative': '', 'characters': [], 'loras': [],
        'prompt_tags': [], 'technical': {}, 'character_options': {},
    }
    try:
        if image.getexif().get(274) in (5, 6, 7, 8):
            result['width'], result['height'] = result['height'], result['width']
    except Exception:
        pass
    if info.get('Software') == 'NovelAI':
        data = json_object(info.get('Comment'))
        positive_v4, negative_v4 = data.get('v4_prompt'), data.get('v4_negative_prompt')
        pos_base, neg_base = _caption(positive_v4).get('base_caption'), _caption(negative_v4).get('base_caption')
        result.update(
            source='NovelAI',
            # Sourceに実記録がある。hashからFull/Curatedを推測しない。
            model=text(data.get('model')) or text(info.get('Source')),
            positive=pos_base if isinstance(pos_base, str) else text(data.get('prompt')) or text(info.get('Description')),
            negative=neg_base if isinstance(neg_base, str) else text(data.get('uc')) or text(data.get('negative_prompt')),
        )
        positives, negatives = _char_captions(positive_v4), _char_captions(negative_v4)
        for index in range(max(len(positives), len(negatives))):
            pos = positives[index] if index < len(positives) else {}
            neg = negatives[index] if index < len(negatives) else {}
            result['characters'].append({
                'index': index + 1,
                'positive': text(pos.get('char_caption')),
                'negative': text(neg.get('char_caption')),
                'centers': _centers(pos.get('centers')),
                'negative_centers': _centers(neg.get('centers')),
            })
        for key in ('use_coords', 'use_order'):
            value = record(positive_v4).get(key)
            if isinstance(value, bool):
                result['character_options'][key] = value
        keys = ('sampler', 'steps', 'seed', 'scale', 'noise_schedule', 'cfg_rescale',
                'uncond_scale', 'sm', 'sm_dyn', 'skip_cfg_above_sigma', 'request_type')
        result['technical'] = {key: data[key] for key in keys if key in data and not isinstance(data[key], (dict, list))}
    else:
        try:
            parser = ComfyUIParser if ComfyUIParser.test(image, path) else SdWebUIParser
            parsed = parser.parse(image, path)
            params, raw = parsed.params, parsed.raw_info or ''
            meta, extra = params.meta or {}, params.extra or {}
            pos, neg = _prompt_parts(raw)
            result.update(
                source=text(meta.get('Source Identifier')) or ('Stable Diffusion' if raw else ''),
                model=text(meta.get('Model')) or text(meta.get('model')),
                positive=text(extra.get('pos_prompt_raw')) or pos,
                negative=text(extra.get('neg_prompt_raw')) or neg,
                loras=[{'name': text(x.get('name')), 'strength': x.get('value')}
                       for x in extra.get('lora', []) if isinstance(x, dict) and text(x.get('name'))],
            )
            result['technical'] = {key: value for key, value in meta.items()
                                   if key not in ('Model', 'Source Identifier', 'final_width', 'final_height')
                                   and isinstance(value, (str, int, float, bool))}
            if result['source'] == 'ComfyUI':
                graph = json_object(info.get('prompt'))
                from scripts.iib.parsers.comfyui import _find_sampler_node_id
                sampler = record(record(graph.get(_find_sampler_node_id(graph))).get('inputs'))
                for key in ('seed', 'noise_seed', 'steps', 'cfg', 'sampler_name', 'scheduler', 'denoise'):
                    if isinstance(sampler.get(key), (str, int, float, bool)):
                        result['technical'][key] = sampler[key]
        except Exception:
            # メタデータなしの通常写真を壊れた生成画像として扱わない。
            pass
    # キャラクターpromptも検索対象。negativeはpositiveタグへ混入させない。
    positive_text = '\n'.join([result['positive']] + [x['positive'] for x in result['characters']])
    result['prompt_tags'] = list(dict.fromkeys(parse_prompt(positive_text.replace('\n', ', '))['pos_prompt']))[:512]
    return result


def read_metadata(path: str) -> dict:
    if Path(path).suffix.lower() in VIDEO_EXTENSIONS:
        result = {'source': '', 'model': '', 'positive': '', 'negative': '', 'characters': [],
                  'loras': [], 'prompt_tags': [], 'technical': {}, 'character_options': {}}
        import av
        with av.open(path) as container:
            stream = next((s for s in container.streams if s.type == 'video'), None)
            if stream is None:
                raise ValueError('video stream missing')
            result.update(width=stream.width, height=stream.height)
            if container.duration is not None:
                result['duration'] = container.duration / av.time_base
            # ComfyUI VideoHelperSuite等の埋め込みpromptも同じparserへ渡す。
            embedded = dict(container.metadata)
            if 'comment' in embedded:
                embedded.update(json_object(embedded['comment']))
            if 'prompt' in embedded:
                with Image.new('RGB', (max(1, stream.width), max(1, stream.height))) as proxy:
                    proxy.info = {k: json.dumps(v) if isinstance(v, dict) else v for k, v in embedded.items()}
                    result.update(normalize_image(proxy, path))
        return result
    with Image.open(path) as image:
        return normalize_image(image, path)


def searchable_text(metadata: dict) -> str:
    values = [metadata.get('positive', ''), metadata.get('negative', ''), metadata.get('model', ''), metadata.get('source', '')]
    for character in metadata.get('characters', []):
        values.extend([character.get('positive', ''), character.get('negative', '')])
    values.extend(item.get('name', '') for item in metadata.get('loras', []))
    return '\n'.join(values).casefold()
