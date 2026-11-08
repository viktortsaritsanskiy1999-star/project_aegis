# Project Aegis — Bundled TrueType Medical Fonts

This directory contains standalone, cross-platform TrueType fonts bundled directly with Project Aegis backend to guarantee reliable, high-fidelity rendering of medical observation documents without dependence on host OS font installations (e.g. Windows Fonts).

## Included Fonts
- `DejaVuSans.ttf`: DejaVu Sans Regular
- `DejaVuSans-Bold.ttf`: DejaVu Sans Bold

## License
The DejaVu fonts are distributed under the **Bitstream Vera Fonts License** with DejaVu project additions in the public domain.
The license permits free use, bundling, embedding, modification, and redistribution for both commercial and non-commercial purposes.
See [LICENSE.txt](./LICENSE.txt) for the full license terms.

## Medical Symbol Coverage
DejaVu Sans provides native Unicode coverage for critical clinical symbols, units, and notations:
- Micro signs: `µg/L`, `µmol/L`, `µIU/mL` (U+00B5, U+03BC)
- Math comparison & tolerance: `≥`, `≤`, `±`, `≈`, `≠`, `>`, `<`
- Temperatures & vital metrics: `°C`, `SpO₂` (U+00B0, U+2082)
- Exponents & surface areas: `m²`, `m³` (U+00B2, U+00B3)
- Punctuation & typography: `—` (em-dash), `–` (en-dash), `•` (bullet), `…` (ellipsis)
- Ratios and ranges: `2.5–3.0 mg/mmol`
