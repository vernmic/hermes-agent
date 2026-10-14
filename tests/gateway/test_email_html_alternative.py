from email.mime.multipart import MIMEMultipart

from plugins.platforms.email.adapter import _build_email_body


def test_html_alternative_preserves_plain_body_and_escapes_markup():
    body = 'ALERT: <script>bad</script>\n\nRECOMMENDATION: review & verify\n\n```\n<a href="x">\n```'
    msg = MIMEMultipart('mixed')
    _build_email_body(msg, body)
    alternative = msg.get_payload()[0]
    assert alternative.get_content_subtype() == 'alternative'
    plain, html = alternative.get_payload()
    assert plain.get_content_type() == 'text/plain'
    assert plain.get_payload(decode=True).decode('utf-8') == body
    rendered = html.get_payload(decode=True).decode('utf-8')
    assert html.get_content_type() == 'text/html'
    assert '<script>' not in rendered
    assert '&lt;script&gt;' in rendered
    assert 'review &amp; verify' in rendered
    assert '<pre><code>&lt;a href="x"&gt;</code></pre>' in rendered
