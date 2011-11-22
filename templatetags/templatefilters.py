from google.appengine.ext import webapp
from django.utils.safestring import mark_safe
import markdown as markdown_module

MARKDOWN_EXTENSIONS = ('codehilite', 'fenced_code')

def markdown(s):
    """Formats the text with Markdown syntax.

    Removes any HTML in the source text.
    """
    md = markdown_module.Markdown(MARKDOWN_EXTENSIONS, safe_mode='remove')
    return mark_safe(md.convert(s))
