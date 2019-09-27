import os
import numpy as np

from .column import str_type
from .stat import _Statistic


on_rtd = os.environ.get('READTHEDOCS', None) == 'True'
if not on_rtd:
    import vaex.superagg


aggregates = {}


def register(f, name=None):
    name = name or f.__name__
    aggregates[name] = f
    return f


class AggregatorDescriptor(object):
    def __repr__(self):
        return 'vaex.agg.{}({!r})'.format(self.short_name, str(self.expression))

    def finish(self, value):
        return value


class AggregatorDescriptorBasic(AggregatorDescriptor):
    def __init__(self, name, expression, short_name, multi_args=False, agg_args=[], selection=None):
        self.name = name
        self.short_name = short_name
        self.expression = str(expression)
        self.agg_args = agg_args
        self.selection = selection
        if not multi_args:
            if self.expression == '*':
                self.expressions = []
            else:
                self.expressions = [self.expression]
        else:
            self.expressions = expression

    def pretty_name(self, id=None):
        id = id or "_".join(map(str, self.expression))
        return '{0}_{1}'.format(id, self.short_name)

    def add_operations(self, agg_task, edges=True, **kwargs):
        value = agg_task.add_aggregation_operation(self, edges=edges, selection=self.selection, **kwargs)
        @vaex.delayed
        def finish(value):
            return self.finish(value)
        return finish(value)

    def _create_operation(self, df, grid):
        if self.expression == '*':
            self.dtype_in = np.dtype('int64')
            self.dtype_out = np.dtype('int64')
        else:
            self.dtype_in = df[str(self.expressions[0])].dtype
            self.dtype_out = self.dtype_in
            if self.short_name == "count":
                self.dtype_out = np.dtype('int64')
        agg_op_type = vaex.utils.find_type_from_dtype(vaex.superagg, self.name + "_", self.dtype_in)
        agg_op = agg_op_type(grid, *self.agg_args)
        return agg_op

    def reduce(self, agg_operations, edges=False):
        agg0 = agg_operations[0]
        agg0.reduce(agg_operations[1:])
        grid = np.asarray(agg0)
        if not edges:
            grid = vaex.utils.extract_central_part(grid)
        return grid

class AggregatorDescriptorNUnique(AggregatorDescriptorBasic):
    def __init__(self, name, expression, short_name, dropmissing, dropnan, selection=None):
        super(AggregatorDescriptorNUnique, self).__init__(name, expression, short_name, selection=selection)
        self.dropmissing = dropmissing
        self.dropnan = dropnan

    def _create_operation(self, df, grid):
        self.dtype_in = df[str(self.expressions[0])].dtype
        self.dtype_out = np.dtype('int64')
        agg_op_type = vaex.utils.find_type_from_dtype(vaex.superagg, self.name + "_", self.dtype_in)
        agg_op = agg_op_type(grid, self.dropmissing, self.dropnan)
        return agg_op

    def reduce(self, agg_operations, edges=False):
        agg0 = agg_operations[0]
        agg0.reduce(agg_operations[1:])
        grid = np.asarray(agg0)
        if not edges:
            grid = vaex.utils.extract_central_part(grid)
        return grid


class AggregatorDescriptorMulti(AggregatorDescriptor):
    """Uses multiple operations/aggregation to calculate the final aggretation"""
    def __init__(self, name, expression, short_name, selection=None):
        self.name = name
        self.short_name = short_name
        self.expression = expression
        self.expressions = [self.expression]
        self.selection = selection

    def pretty_name(self, id=None):
        id = id or "_".join(map(str, self.expression))
        return '{0}_{1}'.format(id, self.short_name)


class AggregatorDescriptorMean(AggregatorDescriptorMulti):
    def __init__(self, name, expression, short_name="mean", selection=None):
        super(AggregatorDescriptorMean, self).__init__(name, expression, short_name, selection=selection)

    def add_operations(self, agg_task, **kwargs):
        expression = expression_sum = expression = agg_task.df[str(self.expression)]
        # ints, floats and bools are upcasted
        if expression_sum.dtype.kind in "buif":
            expression = expression_sum = expression_sum.astype('float64')

        sum_agg = sum(expression_sum, selection=self.selection)
        count_agg = count(expression, selection=self.selection)

        task_sum = sum_agg.add_operations(agg_task, **kwargs)
        task_count = count_agg.add_operations(agg_task, **kwargs)
        self.dtype_in = sum_agg.dtype_in
        self.dtype_out = sum_agg.dtype_out

        @vaex.delayed
        def finish(sum, count):
            dtype = sum.dtype
            if sum.dtype.kind == 'M':
                sum = sum.view('uint64')
                count = count.view('uint64')
            with np.errstate(divide='ignore', invalid='ignore'):
                mean = sum / count
            if dtype.kind != mean.dtype.kind:
                # TODO: not sure why view does not work
                mean = mean.astype(dtype)
            return mean

        return finish(task_sum, task_count)


class AggregatorDescriptorVar(AggregatorDescriptorMulti):
    def __init__(self, name, expression, short_name="var", ddof=0, selection=None):
        super(AggregatorDescriptorVar, self).__init__(name, expression, short_name, selection=selection)
        self.ddof = ddof

    def add_operations(self, agg_task, **kwargs):
        expression_sum = expression = agg_task.df[str(self.expression)]
        expression = expression_sum = expression.astype('float64')
        sum_moment = _sum_moment(str(expression_sum), 2, selection=self.selection)
        sum_ = sum(str(expression_sum), selection=self.selection)
        count_ = count(str(expression), selection=self.selection)

        task_sum_moment = sum_moment.add_operations(agg_task, **kwargs)
        task_sum = sum_.add_operations(agg_task, **kwargs)
        task_count = count_.add_operations(agg_task, **kwargs)
        self.dtype_in = sum_.dtype_in
        self.dtype_out = sum_.dtype_out
        @vaex.delayed
        def finish(sum_moment, sum, count):
            dtype = sum.dtype
            if sum.dtype.kind == 'M':
                sum = sum.view('uint64')
                sum_moment = sum_moment.view('uint64')
                count = count.view('uint64')
            with np.errstate(divide='ignore', invalid='ignore'):
                mean = sum / count
                raw_moments2 = sum_moment/count
                variance = (raw_moments2 - mean**2) #* count/(count-self.ddof)
            if dtype.kind != mean.dtype.kind:
                # TODO: not sure why view does not work
                variance = variance.astype(dtype)
            return self.finish(variance)
        return finish(task_sum_moment, task_sum, task_count)


class AggregatorDescriptorStd(AggregatorDescriptorVar):
    def finish(self, value):
        return value**0.5

@register
def count(expression='*', selection=None):
    '''Creates a count aggregation'''
    return AggregatorDescriptorBasic('AggCount', expression, 'count', selection=selection)

@register
def sum(expression, selection=None):
    '''Creates a sum aggregation'''
    return AggregatorDescriptorBasic('AggSum', expression, 'sum', selection=selection)

@register
def mean(expression, selection=None):
    '''Creates a mean aggregation'''
    return AggregatorDescriptorMean('mean', expression, 'mean', selection=selection)

@register
def min(expression, selection=None):
    '''Creates a min aggregation'''
    return AggregatorDescriptorBasic('AggMin', expression, 'min', selection=selection)

@register
def _sum_moment(expression, moment, selection=None):
    '''Creates a sum of moment aggregator'''
    return AggregatorDescriptorBasic('AggSumMoment', expression, 'summoment', agg_args=[moment], selection=selection)

@register
def max(expression, selection=None):
    '''Creates a max aggregation'''
    return AggregatorDescriptorBasic('AggMax', expression, 'max', selection=selection)

@register
def first(expression, order_expression, selection=None):
    '''Creates a max aggregation'''
    return AggregatorDescriptorBasic('AggFirst', [expression, order_expression], 'first', multi_args=True, selection=selection)

@register
def std(expression, ddof=0, selection=None):
    '''Creates a standard deviation aggregation'''
    return AggregatorDescriptorStd('std', expression, 'std', ddof=ddof, selection=selection)

@register
def var(expression, ddof=0, selection=None):
    '''Creates a variance aggregation'''
    return AggregatorDescriptorVar('var', expression, 'var', ddof=ddof, selection=selection)

def nunique(expression, dropna=False, dropnan=False, dropmissing=False, selection=None):
    """Aggregator that calculates the number of unique items per bin.

    :param expression: Expression for which to calculate the unique items
    :param dropmissing: do not count missing values
    :param dropnan: do not count nan values
    :param dropna: short for any of the above, (see :func:`Expression.isna`)
    """
    if dropna:
        dropnan = True
        dropmissing = True
    return AggregatorDescriptorNUnique('AggNUnique', expression, 'nunique', dropmissing, dropnan, selection=selection)

# @register
# def covar(x, y):
#     '''Creates a standard deviation aggregation'''
#     return _Statistic('covar', x, y)

# @register
# def correlation(x, y):
#     '''Creates a standard deviation aggregation'''
#     return _Statistic('correlation', x, y)

