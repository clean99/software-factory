# Display typeface

The landing page and the status board set their headings in Big Shoulders, by Patric King and the Big Shoulders Project Authors, under the SIL Open Font License 1.1 (`OFL.txt`).

The two `.woff2.b64` files are static instances of the upstream variable font (`ofl/bigshoulders/BigShoulders[opsz,wght].ttf` in github.com/google/fonts) at optical size 72 and weights 600 and 800, cut down to Basic Latin and a few punctuation marks, saved as WOFF2 and base64-encoded. `metrics_site.py` inlines them into each page, so the site makes no font request.

To rebuild one with fontTools and brotli installed:

```sh
python3 - BigShoulders.ttf out.woff2 800 <<'PY'
import sys
from fontTools import subset
from fontTools.ttLib import TTFont
from fontTools.varLib import instancer

source, target, weight = sys.argv[1], sys.argv[2], float(sys.argv[3])
font = instancer.instantiateVariableFont(TTFont(source), {"opsz": 72, "wght": weight})
options = subset.Options()
options.flavor, options.hinting, options.notdef_outline = "woff2", False, True
options.layout_features, options.name_IDs = ["kern", "liga", "tnum", "lnum"], [1, 2, 4, 6]
chooser = subset.Subsetter(options)
chooser.populate(text="".join(chr(code) for code in range(0x20, 0x7F)) + "·–—’‘“”→%×≥…")
chooser.subset(font)
subset.save_font(font, target, options)
PY
base64 -i out.woff2 | tr -d '\n' > bigshoulders-800.woff2.b64
```

When the files are missing the pages fall back to a condensed system face.
