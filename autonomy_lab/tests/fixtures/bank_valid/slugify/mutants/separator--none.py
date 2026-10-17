def slug(text):
    if text == "":
        raise ValueError("text must not be empty")
    return text.strip().lower().replace(" ", "")
