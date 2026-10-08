MODIFIERS = frozenset({"Shift", "Control", "Alt", "Meta", "ControlOrMeta"})


def one_key(key: str) -> bool:
    """Whether `key` is what `BrowserSession.press` takes: one key, with only
    modifiers held down before it."""
    *held, pressed = press_keys(key)
    return bool(pressed) and set(held) <= MODIFIERS


def press_keys(key: str) -> list[str]:
    """`key` split into the keys Playwright's press holds down and then the
    key it presses, as Playwright 1.63's Keyboard.press splits it: a `+`
    ends a key only after one, so `Shift++` is Shift and `+`."""
    keys: list[str] = []
    building = ""
    for char in key:
        if char == "+" and building:
            keys.append(building)
            building = ""
        else:
            building += char
    return [*keys, building]
