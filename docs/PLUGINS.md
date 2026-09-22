# Plugin SDK

Plugins are loaded only from the local plugins folder (*Settings → Plugins → Open plugins folder*;
`<user data>/plugins/`). Nothing is downloaded automatically. A plugin that fails validation is reported
in Settings and in `timelinerx themes`; it never stops the application.

## Theme plugins — `plugins/themes/*.json`

A theme is data, not code. Copy `assets/themes/light.json` as a starting point.

```json
{
  "id": "my_sepia", "name": "My Sepia", "base": "light",
  "grading": [
    {"node": "normalize", "saturation": 0.3, "contrast": 1.05},
    {"node": "tone_curve", "points": [[0, 0.05], [0.5, 0.52], [1, 0.95]]},
    {"node": "duotone", "dark": "#2b1d0e", "light": "#f3e3c6", "amount": 0.6},
    {"node": "color_balance", "shadows": [0.01, 0, -0.01], "highlights": [0.01, 0.005, 0], "preserve_luminance": true}
  ],
  "post": [{"node": "vignette", "strength": 0.3, "radius": 0.7, "softness": 0.6}, {"node": "grain", "amount": 0.01}],
  "palette": {"background": "#efe4cf", "route": "#8a3b12", "route_glow": "#c0602a", "trail_old": "#8a3b12",
              "marker_core": "#2b1d0e", "marker_ring": "#8a3b12", "text_primary": "#2b1d0e",
              "text_secondary": "#5a4630", "card_bg": "#fbf4e6e6", "card_border": "#00000018",
              "attribution": "#2b1d0ec8", "grid": "#00000014"},
  "glow": {"route": 0.4, "marker": 0.5}
}
```

Grading nodes (pointwise, applied once per tile): `normalize` (contrast, saturation, brightness, gamma),
`tone_curve` (monotone cubic through `points`; optional per-channel `red`/`green`/`blue`),
`color_balance` (shadows/midtones/highlights offsets), `duotone`, `invert`.
Post nodes (per frame): `vignette`, `grain`. `base` selects the CARTO style (light/dark) when the
provider is `carto`. A plugin theme may not reuse a built-in id.

## Map-provider plugins — `plugins/providers/*.py`

Python plugins execute code, so they load only when *Settings → Plugins → Load map-provider plugins* is on.

```python
from timelinerx.maps.tiles import RateLimitPolicy, TileResult
import urllib.request

PROVIDER_ID = "my-tiles"            # must not collide with built-in ids

class MyTiles:
    id = "my-tiles"; name = "My tile server"; max_zoom = 18; tile_size = 256; base_flavor = "light"

    def fetch_tile(self, z: int, x: int, y: int) -> TileResult:
        try:
            with urllib.request.urlopen(f"https://tiles.example.org/{z}/{x}/{y}.png", timeout=15) as r:
                return TileResult(True, r.read())
        except OSError as e:
            return TileResult(False, error=str(e))

    def attribution(self) -> str:        # required, non-empty; rendered into every frame
        return "© Example contributors"

    def rate_limit(self) -> RateLimitPolicy:
        return RateLimitPolicy(requests_per_second=4, max_concurrency=2)

def create_provider():
    return MyTiles()
```

Validation checks the attributes, method presence, a non-empty attribution, `RateLimitPolicy` type,
`max_zoom` in 1–24, `tile_size` 256 or 512, and `base_flavor`. Use the id in a project
(`"map_provider": "my-tiles"`). Respect the tile server's usage policy.
