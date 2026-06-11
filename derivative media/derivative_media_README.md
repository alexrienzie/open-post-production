# derivative media
*Proxies and extracted media for the sample assets.*

In the full workflow this folder holds the 720p H.264 proxies, extracted WAVs, and stills derived from the camera originals (the originals themselves live on the RAID master, not here). The public repo ships only a small set of **sample** proxies under `sample/`.

The catalog's `proxy.path` resolves through [`_index/asset_map.json`](_index/asset_map.json) (`asset_id` → relative path); see `dataset/_scripts/workspace_paths.py::resolve_proxy_path`.
