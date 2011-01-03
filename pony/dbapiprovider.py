import sys, threading
from operator import attrgetter

from pony.sqlsymbols import *

def quote_name(name, quote_char='"'):
    if isinstance(name, basestring):
        name = name.replace(quote_char, quote_char+quote_char)
        return quote_char + name + quote_char
    return '.'.join(quote_name(item, quote_char) for item in name)

class AstError(Exception): pass

class Param(object):
    __slots__ = 'style', 'id', 'key',
    def __init__(param, paramstyle, id, key):
        param.style = paramstyle
        param.id = id
        param.key = key
    def __unicode__(param):
        paramstyle = param.style
        if paramstyle == 'qmark': return u'?'
        elif paramstyle == 'format': return u'%s'
        elif paramstyle == 'numeric': return u':%d' % param.id
        elif paramstyle == 'named': return u':p%d' % param.id
        elif paramstyle == 'pyformat': return u'%%(p%d)s' % param.id
        else: raise NotImplementedError
    def __repr__(param):
        return '%s(%r)' % (param.__class__.__name__, param.key)

class Value(object):
    __slots__ = 'value',
    def __init__(self, value):
        self.value = value
    def __unicode__(self):
        value = self.value
        if value is None: return 'null'
        if isinstance(value, (int, long)): return str(value)
        if isinstance(value, basestring): return self.quote_str(value)
        assert False
    def __repr__(self):
        return '%s(%r)' % (self.__class__.__name__, self.value)
    def quote_str(self, s):
        return "'%s'" % s.replace("'", "''")

def flat(tree):
    stack = [ tree ]
    result = []
    stack_pop = stack.pop
    stack_extend = stack.extend
    result_append = result.append
    while stack:
        x = stack_pop()
        if isinstance(x, basestring): result_append(x)
        else:
            try: stack_extend(reversed(x))
            except TypeError: result_append(x)
    return result

def join(delimiter, items):
    items = iter(items)
    try: result = [ items.next() ]
    except StopIteration: return []
    for item in items:
        result.append(delimiter)
        result.append(item)
    return result

def make_binary_op(symbol):
    def binary_op(self, expr1, expr2):
        return '(', self(expr1), symbol, self(expr2), ')'
    return binary_op

def make_unary_func(symbol):
    def unary_func(self, expr):
        return '%s(' % symbol, self(expr), ')'
    return unary_func

class SQLBuilder(object):
    make_param = Param
    value = Value
    def __init__(self, ast, paramstyle='qmark', quote_char='"'):
        self.ast = ast
        self.paramstyle = paramstyle
        self.quote_char = quote_char
        self.keys = {}
        self.result = flat(self(ast))
        self.sql = u''.join(map(unicode, self.result))
        if paramstyle in ('qmark', 'format'):
            layout = tuple(x.key for x in self.result if isinstance(x, Param))
            def adapter(values):
                return tuple(map(values.__getitem__, layout))
        elif paramstyle in ('named', 'pyformat'):
            layout = tuple(param.key for param in sorted(self.keys.itervalues(), key=attrgetter('id')))
            def adapter(values):
                return dict(('p%d'%(i+1), values[key]) for i, key in enumerate(layout))
        elif paramstyle == 'numeric':
            layout = tuple(param.key for param in sorted(self.keys.itervalues(), key=attrgetter('id')))
            def adapter(values):
                return tuple(map(values.__getitem__, layout))
        else: raise NotImplementedError
        self.layout = layout
        self.adapter = adapter 
    def __call__(self, ast):
        if isinstance(ast, basestring):
            raise AstError('An SQL AST list was expected. Got string: %r' % ast)
        symbol = ast[0]
        if not isinstance(symbol, basestring):
            raise AstError('Invalid node name in AST: %r' % ast)
        method = getattr(self, symbol, None)
        if method is None: raise AstError('Method not found: %s' % symbol)
        try:
            return method(*ast[1:])
        except TypeError:
            raise
##            traceback = sys.exc_info()[2]
##            if traceback.tb_next is None:
##                del traceback
##                raise AstError('Invalid data for method %s: %r'
##                               % (symbol, ast[1:]))
##            else:
##                del traceback
##                raise
    def quote_name(self, name):
        return quote_name(name, self.quote_char)
    def INSERT(self, table_name, columns, values):
        return [ 'INSERT INTO ', self.quote_name(table_name), ' (',
                 join(', ', [self.quote_name(column) for column in columns ]),
                 ') VALUES (', join(', ', [self(value) for value in values]), ')' ]
    def UPDATE(self, table_name, pairs, where=None):
        return [ 'UPDATE ', self.quote_name(table_name), '\nSET ',
                 join(', ', [ (self.quote_name(name), '=', self(param)) for name, param in pairs]),
                 where and [ '\n', self(where) ] or [] ]
    def DELETE(self, table_name, where=None):
        result = [ 'DELETE FROM ', self.quote_name(table_name) ]
        if where: result += [ '\n', self(where) ]
        return result
    def SELECT(self, *sections):
        return [ self(s) for s in sections ]
    def ALL(self, *expr_list):
        exprs = [ self(e) for e in expr_list ]
        return 'SELECT ', join(', ', exprs), '\n'
    def DISTINCT(self, *expr_list):
        exprs = [ self(e) for e in expr_list ]
        return 'SELECT DISTINCT ', join(', ', exprs), '\n'
    def AGGREGATES(self, *expr_list):
        exprs = [ self(e) for e in expr_list ]
        return 'SELECT ', join(', ', exprs), '\n'
    def compound_name(self, name_parts):
        return '.'.join(p and self.quote_name(p) or '' for p in name_parts)
    def sql_join(self, join_type, sources):
        result = ['FROM ']
        for i, source in enumerate(sources):
            if len(source) == 3:   alias, kind, x = source; join_cond = None
            elif len(source) == 4: alias, kind, x, join_cond = source
            else: raise AstError('Invalid source in FROM section: %r' % source)
            if alias is not None: alias = self.quote_name(alias)
            if i > 0:
                if join_cond is None: result.append(', ')
                else: result.append(' %s JOIN ' % join_type)
            if kind == TABLE:
                if isinstance(x, basestring): result.append(self.quote_name(x))
                else: result.append(self.compound_name(x))
                if alias is not None: result += ' AS ', alias
            elif kind == SELECT:
                if alias is None: raise AstError('Subquery in FROM section must have an alias')
                result += '(', self.SELECT(*x), ') AS ', alias
            else: raise AstError('Invalid source kind in FROM section: %s',kind)
            if join_cond is not None: result += ' ON ', self(join_cond)
        result.append('\n')
        return result
    def FROM(self, *sources):
        return self.sql_join('INNER', sources)
    def LEFT_JOIN(self, *sources):
        return self.sql_join('LEFT', sources)
    def WHERE(self, condition):
        return 'WHERE ', self(condition), '\n'
    def UNION(self, kind, *sections):
        return 'UNION ', kind, '\n', self.SELECT(*sections)
    def INTERSECT(self, *sections):
        return 'INTERSECT\n', self.SELECT(*sections)
    def EXCEPT(self, *sections):
        return 'EXCEPT\n', self.SELECT(*sections)
    def ORDER_BY(self, *order_list):
        result = ['ORDER BY ']
        for i, (expr, dir) in enumerate(order_list):
            if i > 0: result.append(', ')
            result += self(expr), ' ', dir
        result.append('\n')
        return result
    def LIMIT(self, limit, offset=None):
        if not offset: return 'LIMIT ', self(limit), '\n'
        else: return 'LIMIT ', self(limit), ' OFFSET ', self(offset), '\n'
    def COLUMN(self, table_alias, col_name):
        if table_alias: return [ '%s.%s' % (self.quote_name(table_alias), self.quote_name(col_name)) ]
        else: return [ '%s' % (self.quote_name(col_name)) ]
    def PARAM(self, key):
        keys = self.keys
        param = keys.get(key)
        if param is None:
            param = Param(self.paramstyle, len(keys) + 1, key)
            keys[key] = param
        return [ param ]
    def VALUE(self, value):
        return [ self.value(value) ]
    def AND(self, *cond_list):
        cond_list = [ self(condition) for condition in cond_list ]
        return '(', join(' AND ', cond_list), ')'
    def OR(self, *cond_list):
        cond_list = [ self(condition) for condition in cond_list ]
        return '(', join(' OR ', cond_list), ')'
    def NOT(self, condition):
        return 'NOT (', self(condition), ')'
    
    EQ  = make_binary_op(' = ')
    NE  = make_binary_op(' <> ')
    LT  = make_binary_op(' < ')
    LE  = make_binary_op(' <= ')
    GT  = make_binary_op(' > ')
    GE  = make_binary_op(' >= ')
    ADD = make_binary_op(' + ')
    SUB = make_binary_op(' - ')
    MUL = make_binary_op(' * ')
    DIV = make_binary_op(' / ')
    POW = make_binary_op(' ** ')
    CONCAT = make_binary_op(' || ')

    def NEG(self, expr):
        return '-(', self(expr), ')'
    def IS_NULL(self, expr):
        return self(expr), ' IS NULL'
    def IS_NOT_NULL(self, expr):
        return self(expr), ' IS NOT NULL'
    def LIKE(self, expr, template, escape=None):
        result = self(expr), ' LIKE ', self(template)
        if escape: result = result + (' ESCAPE ', self(escape))
        return result
    def NOT_LIKE(self, expr, template, escape=None):
        result = self(expr), ' NOT LIKE ', self.template
        if escape: result = result + (' ESCAPE ', self(escape))
        return result
    def BETWEEN(self, expr1, expr2, expr3):
        return self(expr1), ' BETWEEN ', self(expr2), ' AND ', self(expr3)
    def NOT_BETWEEN(self, expr1, expr2, expr3):
        return self(expr1), ' NOT BETWEEN ', self(expr2), ' AND ', self(expr3)
    def IN(self, expr1, x):
        if not x: raise AstError('Empty IN clause')
        if len(x) == 1 and x[0] == SELECT:
            return self(expr1), ' IN (', self.SELECT(x), ')'
        expr_list = [ self(expr) for expr in x ]
        return self(expr1), ' IN (', join(', ', expr_list), ')'
    def NOT_IN(self, expr1, x):
        if not x: raise AstError('Empty IN clause')
        if len(x) == 1 and x[0] == SELECT:
            return self(expr1), ' NOT IN (', self.SELECT(x), ')'
        expr_list = [ self(expr) for expr in x ]
        return self(expr1), ' NOT IN (', join(', ', expr_list), ')'
    def EXISTS(self, *sections):
        return 'EXISTS (\nSELECT 1 ', self.SELECT(*sections), ')'
    def NOT_EXISTS(self, *sections):
        return 'NOT EXISTS (\nSELECT * ', self.SELECT(*sections), ')'
    def COUNT(self, kind, expr=None):
        if kind == 'ALL':
            if expr is None: return ['COUNT(*)']
            return 'COUNT(', self(expr), ')'
        elif kind == 'DISTINCT':
            if expr is None: raise AstError('COUNT(DISTINCT) without argument')
            return 'COUNT(DISTINCT ', self(expr), ')'
        raise AstError('Invalid COUNT kind (must be ALL or DISTINCT)')
    SUM = make_unary_func('SUM')
    MIN = make_unary_func('MIN')
    MAX = make_unary_func('MAX')
    AVG = make_unary_func('AVG')
    UPPER = make_unary_func('upper')
    LOWER = make_unary_func('lower')
    LENGTH = make_unary_func('length')
    def SUBSTR(self, expr, start, len=None):
        if len is None: return 'substr(', self(expr), ', ', self(start), ')'
        return 'substr(', self(expr), ', ', self(start), ', ', self(len), ')'
    def CASE(self, expr, cases, default=None):
        result = [ 'case' ]
        if expr is not None:
            result.append(' ')
            result.extend(self(expr))
        for condition, expr in cases:
            result.extend((' when ', self(condition), ' then ', self(expr)))
        if default is not None:
            result.extend((' else ', self(default)))
        result.append(' end')
        return result
    def TRIM(self, expr, chars=None):
        if chars is None: return 'trim(', self(expr), ')'
        return 'trim(', self(expr), ', ', self(chars), ')'
    def LTRIM(self, expr, chars=None):
        if chars is None: return 'ltrim(', self(expr), ')'
        return 'ltrim(', self(expr), ', ', self(chars), ')'
    def RTRIM(self, expr, chars=None):
        if chars is None: return 'rtrim(', self(expr), ')'
        return 'rtrim(', self(expr), ', ', self(chars), ')'
    