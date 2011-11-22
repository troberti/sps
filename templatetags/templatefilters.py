import jinja2
import markdown as markdown_module


MARKDOWN_EXTENSIONS = ('codehilite', 'fenced_code')

def markdown(s):
    """Formats the text with Markdown syntax.

    Removes any HTML in the source text.
    """
    md = markdown_module.Markdown(MARKDOWN_EXTENSIONS, safe_mode='remove')
    return jinja2.Markup(md.convert(s))
