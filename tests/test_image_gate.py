from src.domain.media import AssetVerdict
from src.pipeline.image_gate import GateResult, evaluate
from tests import imagegen

THRESHOLD = 100.0


def _verdict(result: GateResult, asset_id: str) -> AssetVerdict:
    return next(v for v in result.verdicts if v.asset_id == asset_id)


def test_sharp_scores_above_threshold_and_blurry_below() -> None:
    result = evaluate(
        [("s", imagegen.jpeg(imagegen.sharp(1))), ("b", imagegen.jpeg(imagegen.blurry(2)))],
        blur_threshold=THRESHOLD,
    )
    sharp_score = _verdict(result, "s").blur_score
    blurry_score = _verdict(result, "b").blur_score
    assert sharp_score is not None and blurry_score is not None
    assert sharp_score > THRESHOLD > blurry_score
    assert _verdict(result, "b").is_blurry
    assert not _verdict(result, "b").is_kept


def test_near_duplicates_keep_only_the_clearer_one() -> None:
    original = imagegen.sharp(3)
    result = evaluate(
        [
            ("low", imagegen.jpeg(original, 40)),
            ("orig", imagegen.jpeg(original, 95)),
            ("dim", imagegen.jpeg(imagegen.dimmer(original), 90)),
        ],
        blur_threshold=THRESHOLD,
    )
    assert len(result.kept_ids) == 1
    best = max(result.verdicts, key=lambda v: v.blur_score or 0.0)
    assert result.kept_ids == (best.asset_id,)


def test_different_pictures_are_all_kept() -> None:
    images = [(f"p{i}", imagegen.jpeg(imagegen.sharp(10 + i))) for i in range(4)]
    result = evaluate(images, blur_threshold=THRESHOLD)
    assert set(result.kept_ids) == {"p0", "p1", "p2", "p3"}
    assert not result.all_rejected


def test_keeps_only_the_six_sharpest() -> None:
    images = [(f"p{i}", imagegen.jpeg(imagegen.sharp(20 + i))) for i in range(9)]
    result = evaluate(images, blur_threshold=THRESHOLD)
    assert len(result.kept_ids) == 6
    scores = {v.asset_id: v.blur_score or 0.0 for v in result.verdicts}
    kept_scores = [scores[i] for i in result.kept_ids]
    rejected = [scores[v.asset_id] for v in result.verdicts if not v.is_kept]
    assert kept_scores == sorted(kept_scores, reverse=True)
    assert min(kept_scores) >= max(rejected)


def test_all_blurry_batch_is_all_rejected() -> None:
    images = [(f"b{i}", imagegen.jpeg(imagegen.blurry(30 + i))) for i in range(3)]
    result = evaluate(images, blur_threshold=THRESHOLD)
    assert result.kept_ids == ()
    assert result.all_rejected


def test_heic_decodes_to_score_and_phash() -> None:
    result = evaluate([("h", imagegen.heic(imagegen.sharp(40)))], blur_threshold=THRESHOLD)
    verdict = _verdict(result, "h")
    assert verdict.blur_score is not None and verdict.phash is not None
    assert verdict.is_kept


def test_png_decodes() -> None:
    result = evaluate([("p", imagegen.png(imagegen.sharp(41)))], blur_threshold=THRESHOLD)
    assert result.kept_ids == ("p",)


def test_corrupt_bytes_are_rejected_without_raising() -> None:
    result = evaluate(
        [("bad", b"not an image"), ("empty", b""), ("ok", imagegen.jpeg(imagegen.sharp(42)))],
        blur_threshold=THRESHOLD,
    )
    assert result.kept_ids == ("ok",)
    bad = _verdict(result, "bad")
    assert bad.phash is None and bad.blur_score is None and not bad.is_kept


def test_blur_verdict_does_not_depend_on_resolution() -> None:
    for make in (imagegen.sharp, imagegen.blurry):
        small = evaluate([("a", imagegen.png(make(5, 1000)))], blur_threshold=THRESHOLD)
        large = evaluate([("a", imagegen.png(make(5, 3000)))], blur_threshold=THRESHOLD)
        assert _verdict(small, "a").is_blurry == _verdict(large, "a").is_blurry
