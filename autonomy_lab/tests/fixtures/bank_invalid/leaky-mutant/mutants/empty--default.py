def slug(text):
    if text == "":
        return "untitled"
    return text.strip().lower().replace(" ", "-")
