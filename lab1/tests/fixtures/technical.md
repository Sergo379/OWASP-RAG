---
unused: navigation
---
# Technical Security

<!-- remove this navigation
and this comment -->
## Injection

Use **parameters** and `Content-Security-Policy: default-src 'self'`.

- Validate user input.
- Never concatenate SQL.

```sql
-- Keep SQL comments and special symbols:
SELECT * FROM users WHERE name = '<script>' AND id = ?;

<!-- This is literal code, keep it -->
```

### Unicode

Русский текст: ё, безопасность. Symbols: < > & | $ \ `.

    # indented code, not a heading
    print('keep  spaces')
