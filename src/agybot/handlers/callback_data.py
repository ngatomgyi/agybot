"""Callback data constants for Telegram inline keyboards in agybot."""

# History pagination
CB_HISTORY_PREV = "hp:"
CB_HISTORY_NEXT = "hn:"

# Directory browser
CB_DIR_SELECT = "db:sel:"
CB_DIR_UP = "db:up"
CB_DIR_CONFIRM = "db:confirm"
CB_DIR_CANCEL = "db:cancel"
CB_DIR_PAGE = "db:page:"

# Window picker (bind existing unbound window)
CB_WIN_BIND = "wb:sel:"
CB_WIN_NEW = "wb:new"
CB_WIN_CANCEL = "wb:cancel"

# Screenshot
CB_SCREENSHOT_REFRESH = "ss:ref:"

# Interactive UI / Questions
CB_ASK_UP = "aq:up:"
CB_ASK_DOWN = "aq:down:"
CB_ASK_LEFT = "aq:left:"
CB_ASK_RIGHT = "aq:right:"
CB_ASK_ESC = "aq:esc:"
CB_ASK_ENTER = "aq:enter:"
CB_ASK_SPACE = "aq:spc:"
CB_ASK_TAB = "aq:tab:"
CB_ASK_REFRESH = "aq:ref:"

# Session picker (resume existing session)
CB_SESSION_SELECT = "rs:sel:"
CB_SESSION_NEW = "rs:new"
CB_SESSION_CANCEL = "rs:cancel"

# Screenshot control keys
CB_KEYS_PREFIX = "kb:"
