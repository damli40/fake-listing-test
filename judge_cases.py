"""The hand-written answers with known right labels (evalset/). 'tuned' cases were used
while adjusting the matcher and the judge instructions. 'hidden' cases were never tuned on."""
import json
import pathlib

_DIR = pathlib.Path(__file__).resolve().parent / "evalset"
CASES = json.loads((_DIR / "answers-tuned.json").read_text())
HIDDEN = json.loads((_DIR / "answers-hidden.json").read_text())
FETCH_PROOF = json.loads((_DIR / "fetch-proof.json").read_text())
CLAIM_CITED = json.loads((_DIR / "claim-cited.json").read_text())
