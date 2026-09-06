"""An ordinary scikit-learn trainer; OpenFoundry supplies paths and records its outputs."""

import argparse
import json
from pathlib import Path

import joblib
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.naive_bayes import ComplementNB
from sklearn.pipeline import make_pipeline
from sklearn.svm import LinearSVC


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--data", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--features", choices=["word", "char"], required=True)
    parser.add_argument("--classifier", choices=["nb", "svm"], required=True)
    args = parser.parse_args()
    rows = json.loads(args.data.read_text())
    vectorizer = TfidfVectorizer(
        analyzer="char_wb" if args.features == "char" else "word",
        ngram_range=(3, 5) if args.features == "char" else (1, 2),
        sublinear_tf=True,
        min_df=2,
    )
    classifier = ComplementNB(alpha=0.5) if args.classifier == "nb" else LinearSVC(random_state=42)
    model = make_pipeline(vectorizer, classifier)
    model.fit([row["text"] for row in rows], [row["label"] for row in rows])
    joblib.dump(model, args.output)


if __name__ == "__main__":
    main()
