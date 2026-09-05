"""Split SKU dimensions without splitting punctuation inside option labels."""

def split_spec_dimensions(text):
    parts = []
    start = 0
    stack = []
    closing = {"(": ")", "（": "）", "[": "]", "【": "】"}
    for index, char in enumerate(text):
        if char in closing:
            stack.append(closing[char])
        elif stack and char == stack[-1]:
            stack.pop()
        elif not stack and char in "|,，;；>＞":
            parts.append(text[start:index])
            start = index + 1
    parts.append(text[start:])
    return parts
