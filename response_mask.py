import re

def mask_response(text):
    """Mask sensitive data in responses."""
    if not text:
        return text
    # Mask card numbers
    text = re.sub(r'\b\d{12,19}\b', lambda m: m.group()[:6] + '******' + m.group()[-4:], text)
    # Mask CVV
    text = re.sub(r'cvv[:\s]*\d{3,4}', 'cvv:***', text, flags=re.IGNORECASE)
    return text