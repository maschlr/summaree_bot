"""
Ignored users and chats configuration.

Messages from these users/chats will not be processed by the bot.
To add a user or chat to the ignore list:
1. Add their user_id or chat_id to the appropriate list below
2. Commit the change
3. Restart the bot

User IDs are positive integers.
Chat IDs for groups/channels are typically negative integers.
"""

# User IDs to ignore globally
IGNORED_USER_IDS = [
    1087968824
    # 123456789,  # Example: spam user
]

# Chat IDs to ignore globally
IGNORED_CHAT_IDS = [
    # -1001234567890,  # Example: spam group
]
