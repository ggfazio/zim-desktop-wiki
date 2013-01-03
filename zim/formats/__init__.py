# -*- coding: utf-8 -*-

# Copyright 2008-2012 Jaap Karssenberg <jaap.karssenberg@gmail.com>

'''Package with source formats for pages.

Each module in zim.formats should contains exactly one subclass of
DumperClass and exactly one subclass of ParserClass
(optional for export formats). These can be loaded by L{get_parser()}
and L{get_dumper()} respectively. The requirement to have exactly one
subclass per module means you can not import other classes that derive
from these base classes directly into the module.

For format modules it is safe to import '*' from this module.

Parse tree structure
====================

Parse trees are build using the (c)ElementTree module (included in
python 2.5 as xml.etree.ElementTree). It is basically a xml structure
supporting a subset of "html like" tags.

Supported tags:

	- page root element for grouping paragraphs
	- p for paragraphs
	- h for heading, level attribute can be 1..6
	- pre for verbatim paragraphs (no further parsing in these blocks)
	- em for emphasis, rendered italic by default
	- strong for strong emphasis, rendered bold by default
	- mark for highlighted text, renderd with background color or underlined
	- strike for text that is removed, usually renderd as strike through
	- code for inline verbatim text
	- ul for bullet and checkbox lists
	- ol for numbered lists
	- li for list items
	- link for links, attribute href gives the target
	- img for images, attributes src, width, height an optionally href and alt
		- type can be used to control plugin functionality, e.g. type=equation

Unlike html we respect line breaks and other whitespace as is.
When rendering as html use the "white-space: pre" CSS definition to
get the same effect.

Since elements are based on the functional markup instead of visual
markup it is not allowed to nest elements in arbitrary ways.

TODO: allow links to be nested in other elements
TODO: allow strike to have sub elements
TODO: add HR element

If a page starts with a h1 this heading is considered the page title,
else we can fall back to the page name as title.


NOTE: To avoid confusion: "headers" refers to meta data, usually in
the form of rfc822 headers at the top of a page. But "heading" refers
to a title or subtitle in the document.
'''

import re
import string
import logging

import types

from zim.fs import Dir, File
from zim.parsing import link_type, is_url_re, \
	url_encode, url_decode, URL_ENCODE_READABLE
from zim.parser import Builder
from zim.config import data_file
from zim.objectmanager import ObjectManager

import zim.plugins

import zim.notebook # no 'from' to prevent cyclic import errors


logger = logging.getLogger('zim.formats')

# Needed to determine RTL, but may not be available
# if gtk bindings are not installed
try:
	import pango
except:
	pango = None
	logger.warn('Could not load pango - RTL scripts may look bad')

try:
	import xml.etree.cElementTree as ElementTreeModule
except:  # pragma: no cover
	logger.warn('Could not load cElementTree, defaulting to ElementTree')
	import xml.etree.ElementTree as ElementTreeModule


EXPORT_FORMAT = 1
IMPORT_FORMAT = 2
NATIVE_FORMAT = 4
TEXT_FORMAT = 8 # Used for "Copy As" menu - these all prove "text/plain" mimetype

UNCHECKED_BOX = 'unchecked-box'
CHECKED_BOX = 'checked-box'
XCHECKED_BOX = 'xchecked-box'
BULLET = '*' # FIXME make this 'bullet'

FORMATTEDTEXT = 'zim-tree'

HEADING = 'h'
PARAGRAPH = 'p'
VERBATIM_BLOCK = 'pre' # should be same as verbatim
BLOCK = 'div'

IMAGE = 'img'
OBJECT = 'object'

BULLETLIST = 'ul'
NUMBEREDLIST = 'ol'
LISTITEM = 'li'

EMPHASIS = 'emphasis' # TODO change to "em" to be in line with html
STRONG = 'strong'
MARK = 'mark'
VERBATIM = 'code'
STRIKE = 'strike'
SUBSCRIPT = 'sub'
SUPERSCRIPT = 'sup'

LINK = 'link'
TAG = 'tag'
ANCHOR = 'anchor'

BLOCK_LEVEL = (PARAGRAPH, HEADING, VERBATIM_BLOCK, BLOCK, OBJECT, IMAGE, LISTITEM)


def increase_list_iter(listiter):
	'''Get the next item in a list for a numbered list
	E.g if C{listiter} is C{"1"} this function returns C{"2"}, if it
	is C{"a"} it returns C{"b"}.
	@param listiter: the current item, either an integer number or
	single letter
	@returns: the next item, or C{None}
	'''
	try:
		i = int(listiter)
		return str(i + 1)
	except ValueError:
		try:
			i = string.letters.index(listiter)
			return string.letters[i+1]
		except ValueError: # listiter is not a letter
			return None
		except IndexError: # wrap to start of list
			return string.letters[0]



def list_formats(type):
	if type == EXPORT_FORMAT:
		return ['HTML','LaTeX', 'Markdown (pandoc)', 'RST (sphinx)']
	elif type == TEXT_FORMAT:
		return ['Text', 'Wiki', 'Markdown (pandoc)', 'RST (sphinx)']
	else:
		assert False, 'TODO'


def canonical_name(name):
	# "HTML" -> html
	# "Markdown (pandoc)" -> "markdown"
	# "Text" -> "plain"
	name = name.lower()
	if ' ' in name:
		name, _ = name.split(' ', 1)
	if name == 'text': return 'plain'
	else: return name


def get_format(name):
	'''Returns the module object for a specific format.'''
	# If this method is removes, class names in formats/*.py can be made more explicit
	#~ print 'DEPRECATED: get_format() is deprecated in favor if get_parser() and get_dumper()'
	return get_format_module(name)


def get_format_module(name):
	'''Returns the module object for a specific format

	@param name: the format name
	@returns: a module object
	'''
	return zim.plugins.get_module('zim.formats', canonical_name(name))


def get_parser(name, *arg, **kwarg):
	'''Returns a parser object instance for a specific format

	@param name: format name
	@param arg: arguments to pass to the parser object
	@param kwarg: keyword arguments to pass to the parser object

	@returns: parser object instance (subclass of L{ParserClass})
	'''
	module = get_format_module(name)
	klass = zim.plugins.lookup_subclass(module, ParserClass)
	return klass(*arg, **kwarg)


def get_dumper(name, *arg, **kwarg):
	'''Returns a dumper object instance for a specific format

	@param name: format name
	@param arg: arguments to pass to the dumper object
	@param kwarg: keyword arguments to pass to the dumper object

	@returns: dumper object instance (subclass of L{DumperClass})
	'''
	module = get_format_module(name)
	klass = zim.plugins.lookup_subclass(module, DumperClass)
	return klass(*arg, **kwarg)


from xml.etree.ElementTree import TreeBuilder as _TreeBuilder

class TreeBuilder(_TreeBuilder):
	# Hack to deal with API incompatibility between versions of etree
	# Note that cElementTree.TreeBuilder is a function, so we can not
	# subclass it :(  therefore using the python version with the
	# current element class as factory (which might be the c variant)

	def __init__(self):
		_TreeBuilder.__init__(self, Element)

	def start(self, tag, attrs=None):
		if attrs is None:
			attrs = {}
		_TreeBuilder.start(self, tag, attrs)

	def data(self, data):
		assert isinstance(data, basestring), 'Got: %s' % data
		_TreeBuilder.data(self, data)


class ParseTree(ElementTreeModule.ElementTree):
	'''Wrapper for zim parse trees, derives from ElementTree.'''

	def __init__(self, *arg, **kwarg):
		ElementTreeModule.ElementTree.__init__(self, *arg, **kwarg)
		self._object_cache = {}

	@property
	def hascontent(self):
		'''Returns True if the tree contains any content at all.'''
		root = self.getroot()
		return bool(root.getchildren() or root.text)

	@property
	def ispartial(self):
		'''Returns True when this tree is a segment of a page
		(like a copy-paste buffer).
		'''
		return self.getroot().attrib.get('partial', False)

	@property
	def israw(self):
		'''Returns True when this is a raw tree (which is representation
		of TextBuffer, but not really valid).
		'''
		return self.getroot().attrib.get('raw', False)

	def extend(self, tree):
		# Do we need a deepcopy here ?
		myroot = self.getroot()
		otherroot = tree.getroot()
		if otherroot.text:
			children = myroot.getchildren()
			if children:
				last = children[-1]
				last.tail = (last.tail or '') + otherroot.text
			else:
				myroot.text = (myroot.text or '') + otherroot.text

		for element in otherroot.getchildren():
			myroot.append(element)

		return self

	__add__ = extend

	def fromstring(self, string):
		'''Set the contents of this tree from XML representation.'''
		parser = ElementTreeModule.XMLTreeBuilder()
		parser.feed(string)
		root = parser.close()
		self._setroot(root)
		return self # allow ParseTree().fromstring(..)

	def tostring(self):
		'''Serialize the tree to a XML representation.'''
		from cStringIO import StringIO

		# Parent dies when we have attributes that are not a string
		for element in self.getiterator('*'):
			for key in element.attrib.keys():
				element.attrib[key] = str(element.attrib[key])

		xml = StringIO()
		xml.write("<?xml version='1.0' encoding='utf-8'?>\n")
		ElementTreeModule.ElementTree.write(self, xml, 'utf-8')
		return xml.getvalue()

	def copy(self):
		# By using serialization we are absolutely sure all refs are new
		xml = self.tostring()
		return ParseTree().fromstring(xml)

	def write(self, *_):
		'''Writing to file is not implemented, use tostring() instead'''
		raise NotImplementedError

	def parse(self, *_):
		'''Parsing from file is not implemented, use fromstring() instead'''
		raise NotImplementedError

	def _get_heading_element(self, level=1):
		root = self.getroot()
		if root.text and not root.text.isspace():
			return None

		children = root.getchildren()
		if children \
		and children[0].tag == 'h' \
		and children[0].attrib['level'] >= level:
				return children[0]
		else:
			return None

	def get_heading(self, level=1):
		heading_elem = self._get_heading_element(level)
		if heading_elem is not None:
			return heading_elem.text
		else:
			return ""

	def set_heading(self, text, level=1):
		'''Set the first heading of the parse tree to 'text'. If the tree
		already has a heading of the specified level or higher it will be
		replaced. Otherwise the new heading will be prepended.
		'''
		heading = self._get_heading_element(level)
		if heading is not None:
			heading.text = text
		else:
			root = self.getroot()
			heading = ElementTreeModule.Element('h', {'level': level})
			heading.text = text
			heading.tail = root.text
			root.text = None
			root.insert(0, heading)

	def pop_heading(self, level=-1):
		'''If the tree starts with a heading, remove it and any trailing
		whitespace.
		Will modify the tree.
		@returns: a 2-tuple of text and heading level or C{(None, None)}
		'''
		root = self.getroot()
		children = root.getchildren()
		if root.text and not root.text.isspace():
			return None, None

		if children:
			first = children[0]
			if first.tag == 'h':
				mylevel = int(first.attrib['level'])
				if level == -1 or mylevel <= level:
					root.remove(first)
					if first.tail and not first.tail.isspace():
						root.text = first.tail # Keep trailing text
					return first.text, mylevel
				else:
					return None, None
			else:
				return None, None

	def cleanup_headings(self, offset=0, max=6):
		'''Change the heading levels throughout the tree. This makes sure that
		al headings are nested directly under their parent (no gaps in the
		levels of the headings). Also you can set an offset for the top level
		and a max depth.
		'''
		path = []
		for heading in self.getiterator('h'):
			level = int(heading.attrib['level'])
			# find parent header in path using old level
			while path and path[-1][0] >= level:
				path.pop()
			if not path:
				newlevel = offset+1
			else:
				newlevel = path[-1][1] + 1
			if newlevel > max:
				newlevel = max
			heading.attrib['level'] = newlevel
			path.append((level, newlevel))

	def resolve_images(self, notebook=None, path=None):
		'''Resolves the source files for all images relative to a page path	and
		adds a '_src_file' attribute to the elements with the full file path.
		'''
		if notebook is None:
			for element in self.getiterator('img'):
				filepath = element.attrib['src']
				element.attrib['_src_file'] = File(filepath)
		else:
			for element in self.getiterator('img'):
				filepath = element.attrib['src']
				element.attrib['_src_file'] = notebook.resolve_file(element.attrib['src'], path)

	def unresolve_images(self):
		'''Undo effect of L{resolve_images()}, mainly intended for
		testing.
		'''
		for element in self.getiterator('img'):
			if '_src_file' in element.attrib:
				element.attrib.pop('_src_file')

	def encode_urls(self, mode=URL_ENCODE_READABLE):
		'''Calls encode_url() on all links that contain urls.
		See zim.parsing for details. Modifies the parse tree.
		'''
		for link in self.getiterator('link'):
			href = link.attrib['href']
			if is_url_re.match(href):
				link.attrib['href'] = url_encode(href, mode=mode)
				if link.text == href:
					link.text = link.attrib['href']

	def decode_urls(self, mode=URL_ENCODE_READABLE):
		'''Calls decode_url() on all links that contain urls.
		See zim.parsing for details. Modifies the parse tree.
		'''
		for link in self.getiterator('link'):
			href = link.attrib['href']
			if is_url_re.match(href):
				link.attrib['href'] = url_decode(href, mode=mode)
				if link.text == href:
					link.text = link.attrib['href']

	def count(self, text):
		'''Returns the number of occurences of 'text' in this tree.'''
		count = 0
		for element in self.getiterator():
			if element.text:
				count += element.text.count(text)
			if element.tail:
				count += element.tail.count(text)

		return count

	def countre(self, regex):
		'''Returns the number of matches for a regular expression
		in this tree.
		'''
		count = 0
		for element in self.getiterator():
			if element.text:
				newstring, n = regex.subn('', element.text)
				count += n
			if element.tail:
				newstring, n = regex.subn('', element.tail)
				count += n

		return count

	def get_ends_with_newline(self):
		'''Checks whether this tree ends in a newline or not'''
		return self._get_element_ends_with_newline(self.getroot())

	def _get_element_ends_with_newline(self, element):
			if element.tail:
				return element.tail.endswith('\n')
			elif element.tag in ('li', 'h'):
				return True # implicit newline
			else:
				children = element.getchildren()
				if children:
					return self._get_element_ends_with_newline(children[-1]) # recurs
				elif element.text:
					return element.text.endswith('\n')
				else:
					return False # empty element like image

	def visit(self, visitor):
		'''Visit all nodes of this tree

		@note: If the visitor modifies the attrib dict on nodes, this
		will modify the tree.

		@param visitor: a L{Visitor} or L{Builder} object
		'''
		try:
			self._visit(visitor, self.getroot())
		except VisitorStop:
			pass

	def _visit(self, visitor, node):
		try:
			if len(node): # Has children
				visitor.start(node.tag, node.attrib)
				if node.text:
					visitor.text(node.text)
				for child in node:
					self._visit(visitor, child) # recurs
					if child.tail:
						visitor.text(child.tail)
				visitor.end(node.tag)
			else:
				visitor.append(node.tag, node.attrib, node.text)
		except VisitorSkip:
			pass

	def get_objects(self, type=None):
		'''Generator that yields all custom objects in the tree,
		or all objects of a certain type.
		@param type: object type to return or C{None} to get all
		@returns: yields objects (as provided by L{ObjectManager})
		'''
		for elt in self.getiterator(OBJECT):
			if type and elt.attrib.get('type') != type:
				pass
			else:
				obj = self._get_object(elt)
				if obj is not None:
					yield obj

	def _get_object(self, elt):
		## TODO optimize using self._object_cache or new API for
		## passing on objects in the tree
		type = elt.attrib.get('type')
		if elt.tag == OBJECT and type:
			return ObjectManager.get_object(type, elt.attrib, elt.text)
		else:
			return None


class VisitorStop(Exception):
	'''Exception to be raised to cancel a visitor action'''
	pass


class VisitorSkip(Exception):
	'''Exception to be raised when the visitor should skip a leaf node
	and not decent into it.
	'''
	pass


class Visitor(object):
	'''Conceptual opposite of a builder, but with same API.
	Used to walk nodes in a parsetree and call callbacks for each node.
	See e.g. L{ParseTree.visit()}.
	'''

	def start(self, tag, attrib=None):
		'''Start formatted region

		Visitor objects can raise two exceptions in this method
		to influence the tree traversal:

		  1. L{VisitorStop} will cancel the current parsing, but without
			 raising an error. So code implementing a visit method should
			 catch this.
		  2. L{VisitorSkip} can be raised when the visitor wants to skip
			 a node, and should prevent the implementation from further
			 decending into this node

		@note: If the visitor modifies the attrib dict on nodes, this
		will modify the tree. If this is not intended, the implementation
		needs to take care to copy the attrib to break the reference.

		@param tag: the tag name
		@param attrib: optional dict with attributes
		@implementation: optional for subclasses
		'''
		pass

	def text(self, text):
		'''Append text
		@param text: text to be appended as string
		@implementation: optional for subclasses
		'''
		pass

	def end(self, tag):
		'''End formatted region
		@param tag: the tag name
		@raises XXX: when tag does not match current state
		@implementation: optional for subclasses
		'''
		pass

	def append(self, tag, attrib=None, text=None):
		'''Convenience function to open a tag, append text and close
		it immediatly.

		Can raise L{VisitorStop} or L{VisitorSkip}, see C{start()}
		for the conditions.

		@param tag: the tag name
		@param attrib: optional dict with attributes
		@param text: formatted text
		@implementation: optional for subclasses, default implementation
		calls L{start()}, L{text()}, and L{end()}
		'''
		self.start(tag, attrib)
		if text is not None:
			self.text(text)
		self.end(tag)


class ParseTreeBuilder(Builder):
	'''Builder object that builds a L{ParseTree}'''

	def __init__(self, partial=False):
		self.partial = partial
		self._b = ElementTreeModule.TreeBuilder()
		self.stack = [] #: keeps track of current open elements
		self._last_char = None

	def get_parsetree(self):
		'''Returns the constructed L{ParseTree} object.
		Can only be called once, after calling this method the object
		can not be re-used.
		'''
		root = self._b.close()
		return zim.formats.ParseTree(root)

	def start(self, tag, attrib=None):
		self._b.start(tag, attrib)
		self.stack.append(tag)
		if tag in BLOCK_LEVEL:
			self._last_char = None

	def text(self, text):
		self._last_char = text[-1]

		# FIXME hack for backward compat
		if self.stack and self.stack[-1] in (HEADING, LISTITEM):
			text = text.strip('\n')

		self._b.data(text)

	def end(self, tag):
		if tag != self.stack[-1]:
			raise AssertionError, 'Unmatched tag closed: %s' % tag

		if tag in BLOCK_LEVEL:
			if self._last_char is not None and not self.partial:
				#~ assert self._last_char == '\n', 'Block level text needs to end with newline'
				if self._last_char != '\n' and tag not in (HEADING, LISTITEM):
					self._b.data('\n')
					# FIXME check for HEADING LISTITME for backward compat

			# TODO if partial only allow missing \n at end of tree,
			# delay message and trigger if not followed by get_parsetree ?

		self._b.end(tag)
		self.stack.pop()

		# FIXME hack for backward compat
		if tag == HEADING:
			self._b.data('\n')

		self._last_char = None

	def append(self, tag, attrib=None, text=None):
		if tag in BLOCK_LEVEL:
			if text and not text.endswith('\n'):
				text += '\n'

		# FIXME hack for backward compat
		if text and tag in (HEADING, LISTITEM):
			text = text.strip('\n')

		self._b.start(tag, attrib)
		if text:
			self._b.data(text)
		self._b.end(tag)

		# FIXME hack for backward compat
		if tag == HEADING:
			self._b.data('\n')

		self._last_char = None


count_eol_re = re.compile(r'\n+\Z')
split_para_re = re.compile(r'((?:^[ \t]*\n){2,})', re.M)


class OldParseTreeBuilder(object):
	'''This class supplies an alternative for xml.etree.ElementTree.TreeBuilder
	which cleans up the tree on the fly while building it. The main use
	is to normalize the tree that is produced by the editor widget, but it can
	also be used on other "dirty" interfaces.

	This builder takes care of the following issues:
		- Inline tags ('emphasis', 'strong', 'h', etc.) can not span multiple lines
		- Tags can not contain only whitespace
		- Tags can not be empty (with the exception of the 'img' tag)
		- There should be an empty line before each 'h', 'p' or 'pre'
		  (with the exception of the first tag in the tree)
		- The 'p' and 'pre' elements should always end with a newline ('\\n')
		- Each 'p', 'pre' and 'h' should be postfixed with a newline ('\\n')
		  (as a results 'p' and 'pre' are followed by an empty line, the
		  'h' does not end in a newline itself, so it is different)
		- Newlines ('\\n') after a <li> alement are removed (optional)
		- The element '_ignore_' is silently ignored
	'''

	## TODO TODO this also needs to be based on Builder ##

	def __init__(self, remove_newlines_after_li=True):
		assert remove_newlines_after_li, 'TODO'
		self._stack = [] # stack of elements for open tags
		self._last = None # last element opened or closed
		self._data = [] # buffer with data
		self._tail = False # True if we are after an end tag
		self._seen_eol = 2 # track line ends on flushed data
			# starts with "2" so check is ok for first top level element

	def start(self, tag, attrib=None):
		if tag == '_ignore_':
			return self._last
		elif tag == 'h':
			self._flush(need_eol=2)
		elif tag in ('p', 'pre'):
			self._flush(need_eol=1)
		else:
			self._flush()
		#~ print 'START', tag

		if tag == 'h':
			if not (attrib and 'level' in attrib):
				logger.warn('Missing "level" attribute for heading')
				attrib = attrib or {}
				attrib['level'] = 1
		elif tag == 'link':
			if not (attrib and 'href' in attrib):
				logger.warn('Missing "href" attribute for link')
				attrib = attrib or {}
				attrib['href'] = "404"
		# TODO check other mandatory properties !

		if attrib:
			self._last = ElementTreeModule.Element(tag, attrib)
		else:
			self._last = ElementTreeModule.Element(tag)

		if self._stack:
			self._stack[-1].append(self._last)
		else:
			assert tag == 'zim-tree', 'root element needs to be "zim-tree"'
		self._stack.append(self._last)

		self._tail = False
		return self._last

	def end(self, tag):
		if tag == '_ignore_':
			return None
		elif tag in ('p', 'pre'):
			self._flush(need_eol=1)
		else:
			self._flush()
		#~ print 'END', tag

		self._last = self._stack[-1]
		assert self._last.tag == tag, \
			"end tag mismatch (expected %s, got %s)" % (self._last.tag, tag)
		self._tail = True

		if len(self._stack) > 1 and not (tag == 'img' or tag == 'object'
		or (self._last.text and not self._last.text.isspace())
		or self._last.getchildren() ):
			# purge empty tags
			if self._last.text and self._last.text.isspace():
				self._append_to_previous(self._last.text)

			empty = self._stack.pop()
			self._stack[-1].remove(empty)
			children = self._stack[-1].getchildren()
			if children:
				self._last = children[-1]
				if not self._last.tail is None:
					self._data = [self._last.tail]
					self._last.tail = None
			else:
				self._last = self._stack[-1]
				if not self._last.text is None:
					self._data = [self._last.text]
					self._last.text = None

			return empty

		else:
			return self._stack.pop()

	def data(self, text):
		assert isinstance(text, basestring)
		self._data.append(text)

	def _flush(self, need_eol=0):
		# need_eol makes sure previous data ends with \n

		#~ print 'DATA:', self._data
		text = ''.join(self._data)

		# Fix trailing newlines
		if text:
			m = count_eol_re.search(text)
			if m: self._seen_eol = len(m.group(0))
			else: self._seen_eol = 0

		if need_eol > self._seen_eol:
			text += '\n' * (need_eol - self._seen_eol)
			self._seen_eol = need_eol

		# Fix prefix newlines
		if self._tail and self._last.tag in ('h', 'p') \
		and not text.startswith('\n'):
			if text:
				text = '\n' + text
			else:
				text = '\n'
				self._seen_eol = 1
		elif self._tail and self._last.tag == 'li' \
		and text.startswith('\n'):
			text = text[1:]
			if not text.strip('\n'):
				self._seen_eol -=1

		if text:
			assert not self._last is None, 'data seen before root element'
			self._data = []

			# Tags that are not allowed to have newlines
			if not self._tail and self._last.tag in (
			'h', 'emphasis', 'strong', 'mark', 'strike', 'code'):
				# assume no nested tags in these types ...
				if self._seen_eol:
					text = text.rstrip('\n')
					self._data.append('\n' * self._seen_eol)
					self._seen_eol = 0
				lines = text.split('\n')

				for line in lines[:-1]:
					assert self._last.text is None, "internal error (text)"
					assert self._last.tail is None, "internal error (tail)"
					if line and not line.isspace():
						self._last.text = line
						self._last.tail = '\n'
						attrib = self._last.attrib.copy()
						self._last = ElementTreeModule.Element(self._last.tag, attrib)
						self._stack[-2].append(self._last)
						self._stack[-1] = self._last
					else:
						self._append_to_previous(line + '\n')

				assert self._last.text is None, "internal error (text)"
				self._last.text = lines[-1]
			else:
				# TODO split paragraphs

				if self._tail:
					assert self._last.tail is None, "internal error (tail)"
					self._last.tail = text
				else:
					assert self._last.text is None, "internal error (text)"
					self._last.text = text
		else:
			self._data = []


	def close(self):
		assert len(self._stack) == 0, 'missing end tags'
		assert not self._last is None and self._last.tag == 'zim-tree', 'missing root element'
		return self._last

	def _append_to_previous(self, text):
		'''Add text before current element'''
		parent = self._stack[-2]
		children = parent.getchildren()[:-1]
		if children:
			if children[-1].tail:
				children[-1].tail = children[-1].tail + text
			else:
				children[-1].tail = text
		else:
			if parent.text:
				parent.text = parent.text + text
			else:
				parent.text = text


class ParserClass(object):
	'''Base class for parsers

	Each format that can be used natively should define a class
	'Parser' which inherits from this base class.
	'''

	def parse(self, input):
		'''ABSTRACT METHOD: needs to be overloaded by sub-classes.

		This method takes a text or an iterable with lines and returns
		a ParseTree object.
		'''
		raise NotImplementedError

	@classmethod
	def parse_image_url(self, url):
		'''Parse urls style options for images like "foo.png?width=500" and
		returns a dict with the options. The base url will be in the dict
		as 'src'.
		'''
		i = url.find('?')
		if i > 0:
			attrib = {'src': url[:i]}
			for option in url[i+1:].split('&'):
				if option.find('=') == -1:
					logger.warn('Mal-formed options in "%s"' , url)
					break

				k, v = option.split('=')
				if k in ('width', 'height', 'type', 'href'):
					if len(v) > 0:
						attrib[str(k)] = v # str to avoid unicode key
				else:
					logger.warn('Unknown attribute "%s" in "%s"', k, url)
			return attrib
		else:
			return {'src': url}


class DumperClass(Visitor):
	'''Base class for dumper classes.

	Each format that can be used natively should define a class
	'Dumper' which inherits from this base class.

	FIXME FIXME - update docs on how to write a new style dumper using the
	visitor structure - FIXME FIXME
	'''

	TAGS = {} #: dict with formatting tags start and end sequence

	def __init__(self, linker=None, template_options=None):
		self.linker = linker
		self.template_options = template_options or {}
		self._text = []
		self._context = []

	def dump(self, tree):
		'''ABSTRACT METHOD needs to be overloaded by sub-classes.

		This method takes a ParseTree object and returns a list of
		lines of text.
		'''
		# FIXME - issue here is that we need to reset state - should be in __init__
		self._text = []
		self._context = [(None, None, self._text)]
		tree.visit(self)
		assert len(self._context) == 1, 'Unclosed tags on tree'
		#~ import pprint; pprint.pprint(self._text)
		return self.get_lines() # FIXME - maybe just return text ?

	def get_lines(self):
		return u''.join(self._text).splitlines(1)

	def start(self, tag, attrib=None):
		if attrib:
			attrib = attrib.copy() # Ensure dumping does not change tree
		self._context.append((tag, attrib, []))

	def text(self, text):
		assert not text is None
		text = self.encode_text(text)
		self._context[-1][-1].append(text)

	def end(self, tag):
		assert tag and self._context[-1][0] == tag, 'Unmatched tag: %s' % tag
		_, attrib, strings = self._context.pop()
		if tag in self.TAGS:
			assert strings, 'Can not append empty %s element' % tag
			start, end = self.TAGS[tag]
			strings.insert(0, start)
			strings.append(end)
		elif tag == FORMATTEDTEXT:
			pass
		else:
			try:
				method = getattr(self, 'dump_'+tag)
			except AttributeError:
				raise AssertionError, 'BUG: Unknown tag: %s' % tag

			strings = method(tag, attrib, strings)
			#~ try:
				#~ u''.join(strings)
			#~ except:
				#~ print "BUG: %s returned %s" % ('dump_'+tag, strings)

		if strings is not None:
			self._context[-1][-1].extend(strings)

	def append(self, tag, attrib=None, text=None):
		strings = None
		if tag in self.TAGS:
			assert text is not None, 'Can not append empty %s element' % tag
			start, end = self.TAGS[tag]
			text = self.encode_text(text)
			strings = [start, text, end]
		elif tag == FORMATTEDTEXT:
			if text is not None:
				strings = [self.encode_text(text)]
		else:
			if attrib:
				attrib = attrib.copy() # Ensure dumping does not change tree

			try:
				method = getattr(self, 'dump_'+tag)
			except AttributeError:
				raise AssertionError, 'BUG: Unknown tag: %s' % tag

			if text is None:
				strings = method(tag, attrib, None)
			else:
				strings = method(tag, attrib, [self.encode_text(text)])

		if strings is not None:
			self._context[-1][-1].extend(strings)

	def encode_text(self, text):
		'''Optional method to encode text elements in the output
		@param text: text to be encoded
		@returns: encoded text
		@implementation: optional, default just returns unmodified input
		'''
		return text

	def prefix_lines(self, prefix, strings):
		'''Convenience method to wrap a number of lines with e.g. an
		indenting sequence.
		@param prefix: a string to prefix each line
		@param strings: a list of pieces of text
		@returns: a new list of lines, each starting with prefix
		'''
		lines = u''.join(strings).splitlines(1)
		return [prefix + l for l in lines]

	def dump_object(self, tag, attrib, strings=None):
		'''Dumps object using proper ObjectManager'''
		format = str(self.__class__.__module__).split('.')[-1]
		if 'type' in attrib:
			obj = ObjectManager.get_object(attrib['type'], attrib, u''.join(strings))
			output = obj.dump(format, self, self.linker)
			if output is not None:
				return [output]

		return self.dump_object_fallback(tag, attrib, strings)

		# TODO put content in attrib, use text for caption (with full recursion)
		# See img

	def dump_object_fallback(self, tag, attrib, strings=None):
		raise NotImplementedError

	def isrtl(self, text):
		'''Check for Right To Left script
		@param text: the text to check
		@returns: C{True} if C{text} starts with characters in a
		RTL script, or C{None} if direction is not determined.
		'''
		if pango is None:
			return None

		# It seems the find_base_dir() function is not documented in the
		# python language bindings. The Gtk C code shows the signature:
		#
		#     pango.find_base_dir(text, length)
		#
		# It either returns a direction, or NEUTRAL if e.g. text only
		# contains punctuation but no real characters.

		dir = pango.find_base_dir(text, len(text))
		if dir == pango.DIRECTION_NEUTRAL:
			return None
		else:
			return dir == pango.DIRECTION_RTL


class BaseLinker(object):
	'''Base class for linker objects
	Linker object translate links in zim pages to (relative) URLs.
	This is used when exporting data to resolve links.
	Relative URLs start with "./" or "../" and should be interpreted
	in the same way as in HTML. Both URLs and relative URLs are
	already URL encoded.
	'''

	def __init__(self):
		self._icons = {}
		self._links = {}
		self.path = None
		self.usebase = False
		self.base = None

	def set_path(self, path):
		'''Set the page path for resolving links'''
		self.path = path
		self._links = {}

	def set_base(self, dir):
		'''Set a path to use a base for linking files'''
		assert isinstance(dir, Dir)
		self.base = dir

	def set_usebase(self, usebase):
		'''Set whether the format supports relative files links or not'''
		self.usebase = usebase

	def resolve_file(self, link):
		'''Find the source file for an attachment
		Used e.g. by the latex format to find files for equations to
		be inlined. Do not use this method to resolve links, the file
		given here might be temporary and is not guaranteed to be
		available after the export. Use L{link()} or C{link_file()}
		to resolve links to files.
		@returns: a L{File} object or C{None} if no file was found
		@implementation: must be implemented by child classes
		'''
		raise NotImplementedError

	def link(self, link):
		'''Returns an url for a link in a zim page
		This method is used to translate links of any type. It determined
		the link type and dispatches to L{link_page()}, L{link_file()},
		or other C{link_*} methods.

		Results of this method are cached, so only calls dispatch method
		once for repeated occurences. Setting a new path with L{set_path()}
		will clear the cache.

		@param link: link to be translated
		@type link: string

		@returns: url, uri or whatever link notation is relevant in the
		context of this linker
		@rtype: string
		'''
		assert not self.path is None
		if not link in self._links:
			type = link_type(link)
			if type == 'page':    href = self.link_page(link)
			elif type == 'file':  href = self.link_file(link)
			elif type == 'mailto':
				if link.startswith('mailto:'):
					href = self.link_mailto(link)
				else:
					href = self.link_mailto('mailto:' + link)
			elif type == 'interwiki':
				href = zim.notebook.interwiki_link(link)
				if href and href != link:
					href = self.link(href) # recurs
				else:
					logger.warn('No URL found for interwiki link "%s"', link)
					link = href
			elif type == 'notebook':
				href = self.link_notebook(link)
			else: # I dunno, some url ?
				method = 'link_' + type
				if hasattr(self, method):
					href = getattr(self, method)(link)
				else:
					href = link
			self._links[link] = href
		return self._links[link]

	def img(self, src):
		'''Returns an url for image file 'src' '''
		return self.link_file(src)

	def icon(self, name):
		'''Returns an url for an icon'''
		if not name in self._icons:
			self._icons[name] = data_file('pixmaps/%s.png' % name).uri
		return self._icons[name]

	def resource(self, path):
		'''To be overloaded, return an url for template resources'''
		raise NotImplementedError

	def link_page(self, link):
		'''To be overloaded, return an url for a page link
		@implementation: must be implemented by child classes
		'''
		raise NotImplementedError

	def link_file(self, path):
		'''To be overloaded, return an url for a file link
		@implementation: must be implemented by child classes
		'''
		raise NotImplementedError

	def link_mailto(self, uri):
		'''Optional method, default just returns uri'''
		return uri

	def link_notebook(self, url):
		'''Optional method, default just returns url'''
		return url


class StubLinker(BaseLinker):
	'''Linker used for testing - just gives back the link as it was
	parsed. DO NOT USE outside of testing.
	'''

	def __init__(self):
		BaseLinker.__init__(self)
		self.path = '<PATH>'
		self.base = Dir('<NOBASE>')

	def resolve_file(self, link):
		return self.base.file(link)
			# Very simple stub, allows finding files be rel path for testing

	def icon(self, name):
		return 'icon:' + name

	def resource(self, path):
		return path

	def link_page(self, link):
		return link

	def link_file(self, path):
		return path
