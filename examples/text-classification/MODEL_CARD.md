# SMS spam classifier

Detect English-language SMS spam with a small CPU model. Prioritize spam F1 and
keep precision above 95% to limit false alarms. Acceptance criteria and candidate
choices live in `experiment.yaml`; measured results come from OpenFoundry.

Data: Almeida & Hidalgo (2011), [UCI SMS Spam Collection](https://doi.org/10.24432/C5CC84),
CC BY 4.0. Preparation normalizes whitespace and case for deduplication, drops
conflicting labels, and assigns unique messages to an 80/20 split by SHA-256.
Vectorizer fitting uses only the training split. The development split informs
candidate selection; its scores are development evidence, not an untouched test.
No claim is made about contemporary spam, other languages, or deployment quality.

Export includes the fitted pipeline and captured source. `predict.py` accepts the
model path and one or more messages. Joblib models should be loaded from trusted
sources; they contain executable Python serialization.
