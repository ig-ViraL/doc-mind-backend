from services.chunker import TokenChunker, approx_token_count


def words(n, prefix="w"):
    return " ".join(f"{prefix}{i}" for i in range(n))


def test_short_text_is_one_chunk():
    chunks = TokenChunker(50, 10).chunk("Hello world. This is short.", "d")
    assert len(chunks) == 1
    assert chunks[0]["text"] == "Hello world. This is short."


def test_chunks_respect_token_limit_and_never_cut_words():
    text = "\n\n".join(words(40, f"p{j}x") + "." for j in range(10))
    chunker = TokenChunker(60, 15)
    chunks = chunker.chunk(text, "d")
    assert len(chunks) > 1
    vocab = set(text.split())
    for c in chunks:
        assert approx_token_count(c["text"]) <= 60 + 2  # small slack for joining
        for w in c["text"].split():
            assert w in vocab, f"cut word: {w!r}"
        assert text[c["start_char"]:c["end_char"]] == c["text"]


def test_overlap_repeats_trailing_content():
    text = " ".join(f"Sentence number {i} is here." for i in range(60))
    chunks = TokenChunker(40, 12).chunk(text, "d")
    assert len(chunks) > 2
    for a, b in zip(chunks, chunks[1:]):
        assert b["start_char"] < a["end_char"], "consecutive chunks should overlap"


def test_custom_token_counter_is_used():
    calls = []

    def counter(s):
        calls.append(s)
        return len(s.split())

    chunks = TokenChunker(5, 1, token_counter=counter).chunk(words(23), "d")
    assert calls
    assert all(len(c["text"].split()) <= 5 for c in chunks)


def test_offsets_shift():
    spans = TokenChunker(50, 5).split_spans("  abc def  ", offset=100)
    assert spans == [(102, 109)]


def test_empty_text():
    assert TokenChunker(50, 5).chunk("   \n\n ", "d") == []
