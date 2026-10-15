# ComfyUI workflows

- `blackout_dataset_builder.json`: before/after dataset builder for an LTX IC-LoRA. Mode A previews the first frame (stills, ~10 per clip, are made separately). Mode B relights the clip to the chosen still with **LTX 2.3 ref video-to-video on fal** (`fal-ai/ltx-2.3-quality/reference-video-to-video`, backend `ltx23_quality_ref_v2v`, the default); Kling and Wan stay selectable in node B1. Needs the `LTXBlackoutVideoArgsLTX` node from `ComfyUI-ltx-fal-helpers`.
- Needs the `ComfyUI-ltx-fal-helpers` custom nodes and a `FAL_KEY`. No keys or personal paths are stored; clip names are placeholders.

Raw URL: https://raw.githubusercontent.com/cfryant/lora-dataset-review/main/workflows/blackout_dataset_builder.json
