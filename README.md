# BC K–9 Curriculum Database Builder

## Scripts
`build_bc_curriculum_db.py` downloads the official BC "Area of Learning"
elaboration PDFs from [curriculum.gov.bc.ca](https://curriculum.gov.bc.ca) and
parses them into a structured database for the IB PYP Year Planner. It maps each
subject and grade (K–7 by default) to its curricular competencies and content
standards, each with the matching ministry elaborations.

### Requirements

```bash
pip install -r requirements.txt
```

Python 3.10+.

### Usage

```bash
python build_bc_curriculum_db.py                 # build everything, K–7
python build_bc_curriculum_db.py --max-grade 7   # highest grade to keep (K always kept)
python build_bc_curriculum_db.py --subjects mathematics science
python build_bc_curriculum_db.py --no-sqlite     # skip the SQLite export
python build_bc_curriculum_db.py --layout        # alt extraction if a column looks off
python build_bc_curriculum_db.py --force-download # re-download cached PDFs
```

| Flag | Default | Purpose |
|------|---------|---------|
| `--out-dir` | `out` | Where outputs are written. |
| `--max-grade` | `7` | Highest grade to keep. K is always kept. |
| `--subjects` | all | Limit to specific subject slugs. |
| `--layout` | off | Use pdfplumber layout mode (fallback for column issues). |
| `--no-sqlite` | off | Don't write the SQLite database. |
| `--force-download` | off | Re-download PDFs even if cached. |
| `--no-raw` | off | Don't write the raw/debug text dumps. |


### Outputs

Everything lands under `out/` (or `--out-dir`):

```
out/
├── bc_curriculum.json        ← the combined database (load this into the app)
├── bc_curriculum.sqlite       ← same data, normalised SQLite
├── subjects/
│   └── <slug>.json            ← one file per subject (same shape as a subject entry)
├── pdfs/
│   └── <slug>.pdf             ← cached source PDFs (re-runs skip re-download)
└── raw/
    ├── <slug>.txt             ← naive full-text extract (inspection)
    └── <slug>.columns.txt     ← column-separated text per grade band (debugging)
```

The primary artifact is **`out/bc_curriculum.json`**. The `raw/*.columns.txt`
files are diagnostic: each section is labelled by band (e.g. `##### GRADES 6/7
#####`) and shows the separated competency / content / elaboration text, which
is the quickest place to verify a subject parsed correctly.

## JSON structure

`bc_curriculum.json` is a single object:

```jsonc
{
  "source": "https://curriculum.gov.bc.ca",
  "generated": "2026-06-15",          // build date (ISO)
  "max_grade": 7,
  "subjects": {
    "Arts Education": {
      "slug": "arts-education",
      "url": "https://curriculum.gov.bc.ca/.../en_arts-education_k-9_elab.pdf",
      "grades": {
        "K": {
          "big_ideas": [
            "People create art to express who they are as individuals and community"
          ],
          "big_ideas_elaborations": {
            "arts": "includes but is not limited to the four disciplines of dance, drama, music, and visual arts"
          },
          "curricular_competencies": [
            {
              "text": "Explore elements, processes, materials, movements, technologies, tools, and techniques of the arts",
              "group": "Exploring and creating",
              "elaborations": {
                "elements": "characteristics of dance, drama, music, and visual arts"
              }
            }
          ],
          "content": [
            {
              "text": "elements in the arts, including but not limited to: dance: body, space, dynamics, time, relationships, form ...",
              "group": null,
              "elaborations": {
                "dramatic forms": "a medium for the expression of dramatic meaning ..."
              }
            }
          ]
        }
        // "1", "2", ... "7"
      }
    }
    // ... other subjects
  }
}
```
