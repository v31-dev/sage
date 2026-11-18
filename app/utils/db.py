def like_literal(text: str) -> str:
  """`text` for a `LIKE … ESCAPE '\\'`, with LIKE's wildcards in it escaped."""
  return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
