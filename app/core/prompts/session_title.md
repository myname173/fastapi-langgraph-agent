Generate a short title for a conversation based on the user's first message.

Output a json object with a "title" field.

Rules:
- 3 to 6 words maximum
- Match the language of the user's message
- No quotes, no trailing punctuation, no prefixes like "Title:" or "Subject:"
- Capture the specific topic, not a generic description

User message:
{user_message}
