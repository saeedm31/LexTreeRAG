# System architecture slides

**[LexTreeRAG-architecture.pdf](LexTreeRAG-architecture.pdf)**: 12 slides to present the product.
What it does, how a question becomes a cited answer, cost control, and the company setup on AWS
(code and guide on the [`aws` branch](https://github.com/saeedm31/LexTreeRAG/blob/aws/docs/aws.md)).

## Files

| File | What it is |
|---|---|
| `slides/*.html` | One file per slide (1920×1080), in the order listed in `deck.json` |
| `deck.json` | Slide order, title and fonts |
| `icons.json` | The few icons the slides use ([Lucide](https://lucide.dev), ISC licence) |
| `build_pdf.py` | Turns the slides into the PDF with a headless browser |

## Update the PDF

Edit a slide and push to `main`. The **architecture PDF** GitHub Action rebuilds the PDF and commits it.

To build it on your machine:

```bash
pip install playwright && python -m playwright install chromium
python architecture/build_pdf.py
```
