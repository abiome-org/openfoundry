# Evaluation

Evaluation turns a run into immutable evidence: metric thresholds from an
`EvaluationSpec`, compatibility vectors from a `ModelPackage`, and boolean
pass outputs from evaluator stages combine into one `EvaluationResult`.

## Learning from results

Use evaluation to build a better model: inspect failures, change training or
data, tune rewards, and select candidates. Check whether gains translate to the
behavior users need. When a score rewards a shortcut or a verifier accepts an
incorrect answer, fix the evaluation and compare the baseline and candidate
under the revised protocol.

Results that guide training or selection are development evidence. Keep using
them, and record that influence. For an independent estimate of generalization,
measure on fresh or reserved cases that did not guide those decisions. Reusing
a correct metric or verifier across splits is fine; document the data exposure
and feedback that affect the claim.

## Model packages

```yaml
apiVersion: openfoundry.dev/v1alpha1
kind: ModelPackage
metadata:
  name: example-affine
spec:
  signatures:
    input: {type: object, required: [input], properties: {input: {type: number}}}
    output: {type: object, required: [prediction], properties: {prediction: {type: number}}}
    state: {type: object, required: [path], properties: {path: {type: string}}}
  adapters:
    trainingReference: {stage: train, operation: run, config: {action: train}}
    inferenceReference:
      module: modules/examples/affine-serving/module.yaml
      operation: run
      stateOutput: train.model
      config: {}
  compatibilityVectors:
    - name: positive
      method: predict
      inputs: {input: 3.0}
      expected: {prediction: 7.0}
      tolerances: {prediction: {absolute: 0.01, relative: 0.001}}
```

`signatures` are the input, output, and state contracts of the served model.
`trainingReference` must match a workload stage's operation and configuration;
`inferenceReference` names an independent serving module and the stage output
that carries the trained state. Compatibility vectors are inputs and expected
outputs, with optional finite non-negative tolerances and a seed. Both modules
are captured at admission, so the compatibility check later runs the exact
admitted serving module against the trained state. This checks the serving
behavior directly instead of relying solely on the training stage's report.

When `stateOutput` names an artifact, OpenFoundry verifies and restores its bytes before
both compatibility checks and serving. The adapter reads `request.state["path"]`:
a file for a file artifact, or the root directory for a directory artifact.
The state also carries `resource`, `kind`, `artifacts`, and `paths`, as with a
resolved workload artifact input. An output containing an inline state object
is passed through unchanged. State signatures validate this resolved value.

## Evaluation specs

```yaml
apiVersion: openfoundry.dev/v1alpha1
kind: EvaluationSpec
metadata:
  name: example-affine
spec:
  metrics:
    - {name: training-loss, output: train.loss, maximum: 0.000001}
    - {name: parameter-check, output: evaluate.passed, minimum: 1.0}
```

Each metric names a run output and an optional `minimum` or `maximum`. Metric
names must be unique and may not be `passed` or `compatibilityPassed`. Each run
pins its evaluation spec at admission. Revise the spec when it needs improvement;
compare candidates using the same revision so a protocol change is not mistaken
for a model improvement.

## Evaluating a run

```sh
openfoundry --actor research-agent evaluate run/<run-id>
openfoundry resource list --kind EvaluationResult
```

`evaluate` reads the run result, applies every metric threshold, runs the
compatibility vectors through the admitted serving module, collects every
boolean output named `*.passed`, and publishes `EvaluationResult`
`evaluation-<run-id>` with `scores` (each metric, `passed`, and
`compatibilityPassed`), `failures`, and the exact evaluation and model package
revisions it used. A run without a model package passes compatibility only
when an evaluator stage emits `compatibilityPassed: true` itself.

## Experiments

```sh
openfoundry --actor research-agent experiment create longer-training \
  --baseline run/<baseline-run-id> --candidate run/<candidate-run-id> \
  --metric training-loss --direction minimize
```

An `Experiment` compares one numeric metric between two evaluation results
that used the same evaluation revisions and records `baseline`, `candidate`,
or `tie` with the delta. References may be `run/<id>`, an evaluation result
name, or a full `openfoundry://` URI. Statistical treatment, repeats, slices, and
uncertainty belong to evaluator modules and their artifacts; the experiment
decision is only as strong as the metric behind it. Record the conclusion and experiment revision in the model card.
