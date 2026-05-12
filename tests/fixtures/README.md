# Test Fixtures — Regression Corpus

Each PDF here is a frozen reproduction of a real bug or a known-good baseline.
Never replace without keeping the original alongside (rename to `*_legacy.pdf`).

`perils_*` and `ke1335_*` originate from this repo's `input/` directory.
`brownfield.pdf` and `unilever_vitality_*` were sourced from external case
collections (`~/Desktop/DRRC Documents/` and `~/Desktop/untitled folder/`
respectively, on the original maintainer's machine).

| File | Origin filename | Bug category |
|---|---|---|
| perils_regular.pdf            | input/Perils and Pitfalls - Case.pdf                    | Baseline (no bugs reported) |
| perils_watermarked.pdf        | input/Perils and Pitfalls - Watermarked Case (1).pdf    | DNC watermark interaction |
| ke1335_raw.pdf                | input/KE1335_KLA_Tencor_Case_4-1-2026.pdf               | Baseline raw input |
| ke1335_already_accessible.pdf | input/KE1335_Final_20260318_accessible.pdf              | Baseline already-accessible |
| brownfield.pdf                | BettingonBrownfield_HBS_20140314_accessible.pdf         | Horizontal-row shuffle (4 1 / 3 2) |
| unilever_vitality_regular.pdf | Unilever's Mission for Vitality.pdf                     | Kellogg logo → uninformative figure |
| unilever_vitality_dnc.pdf     | Unilever's Mission for Vitality DNC.pdf                 | DNC watermark + grouped images |
