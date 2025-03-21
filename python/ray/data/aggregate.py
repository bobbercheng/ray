import abc
import math
from typing import TYPE_CHECKING, Callable, List, Optional, Any

import numpy as np

from ray.data._internal.util import is_null
from ray.data.block import AggType, Block, BlockAccessor, KeyType, T, U
from ray.util.annotations import PublicAPI, Deprecated

if TYPE_CHECKING:
    from ray.data import Schema


@Deprecated(message="AggregateFn is deprecated, please use AggregateFnV2")
@PublicAPI
class AggregateFn:
    """NOTE: THIS IS DEPRECATED, PLEASE USE AggregateFnV2 INSTEAD

    Defines how to perform a custom aggregation in Ray Data.

    `AggregateFn` instances are passed to a Dataset's ``.aggregate(...)`` method to
    specify the steps required to transform and combine rows sharing the same key.
    This enables implementing custom aggregators beyond the standard
    built-in options like Sum, Min, Max, Mean, etc.

    Args:
        init: Function that creates an initial aggregator for each group. Receives a key
            (the group key) and returns the initial accumulator state (commonly 0,
            an empty list, or an empty dictionary).
        merge: Function that merges two accumulators generated by different workers
            into one accumulator.
        name: An optional display name for the aggregator. Useful for debugging.
        accumulate_row: Function that processes an individual row. It receives the current
            accumulator and a row, then returns an updated accumulator. Cannot be
            used if `accumulate_block` is provided.
        accumulate_block: Function that processes an entire block of rows at once. It receives the
            current accumulator and a block of rows, then returns an updated accumulator.
            This allows for vectorized operations. Cannot be used if `accumulate_row`
            is provided.
        finalize: Function that finishes the aggregation by transforming the final
            accumulator state into the desired output. For example, if your
            accumulator is a list of items, you may want to compute a statistic
            from the list. If not provided, the final accumulator state is returned
            as-is.

    Example:
        .. testcode::

            import ray
            from ray.data.aggregate import AggregateFn

            # A simple aggregator that counts how many rows there are per group
            count_agg = AggregateFn(
                init=lambda k: 0,
                accumulate_row=lambda counter, row: counter + 1,
                merge=lambda c1, c2: c1 + c2,
                name="custom_count"
            )
            ds = ray.data.from_items([{"group": "A"}, {"group": "B"}, {"group": "A"}])
            result = ds.groupby("group").aggregate(count_agg).take_all()
            # result: [{'group': 'A', 'custom_count': 2}, {'group': 'B', 'custom_count': 1}]
    """

    def __init__(
        self,
        init: Callable[[KeyType], AggType],
        merge: Callable[[AggType, AggType], AggType],
        name: str,
        accumulate_row: Callable[[AggType, T], AggType] = None,
        accumulate_block: Callable[[AggType, Block], AggType] = None,
        finalize: Optional[Callable[[AggType], U]] = None,
    ):
        if (accumulate_row is None and accumulate_block is None) or (
            accumulate_row is not None and accumulate_block is not None
        ):
            raise ValueError(
                "Exactly one of accumulate_row or accumulate_block must be provided."
            )

        if accumulate_block is None:

            def accumulate_block(a: AggType, block: Block) -> AggType:
                block_acc = BlockAccessor.for_block(block)
                for r in block_acc.iter_rows(public_row_format=False):
                    a = accumulate_row(a, r)
                return a

        if not isinstance(name, str):
            raise TypeError("`name` must be provided.")

        if finalize is None:
            finalize = lambda a: a  # noqa: E731

        self.name = name
        self.init = init
        self.merge = merge
        self.accumulate_block = accumulate_block
        self.finalize = finalize

    def _validate(self, schema: Optional["Schema"]) -> None:
        """Raise an error if this cannot be applied to the given schema."""
        pass


@PublicAPI(stability="alpha")
class AggregateFnV2(AggregateFn, abc.ABC):
    """Provides an interface to implement efficient aggregations to be applied
    to the dataset.

    `AggregateFnV2` instances are passed to a Dataset's ``.aggregate(...)`` method to
    perform aggregations by applying distributed aggregation algorithm:

        - `aggregate_block` is applied to individual blocks, producing partial
            aggregations.
        - `combine` combines new partially aggregated value (previously returned
            from `aggregate_block` partial aggregations into a singular partial
            aggregation) with the previously stored accumulator.
        - `finalize` transforms partial aggregation into its final state (for
            some aggregations this is an identity transformation, ie no-op)

    """

    def __init__(
        self,
        name: str,
        zero_factory: Callable[[], AggType],
        *,
        on: Optional[str],
        ignore_nulls: bool,
    ):
        if not name:
            raise ValueError(
                f"Non-empty string has to be provided as name (got {name})"
            )

        self._target_col_name = on
        self._ignore_nulls = ignore_nulls

        _safe_combine = _null_safe_combine(self.combine, ignore_nulls)
        _safe_aggregate = _null_safe_aggregate(self.aggregate_block, ignore_nulls)
        _safe_finalize = _null_safe_finalize(self._finalize)

        _safe_zero_factory = _null_safe_zero_factory(zero_factory, ignore_nulls)

        super().__init__(
            name=name,
            init=_safe_zero_factory,
            merge=_safe_combine,
            accumulate_block=lambda _, block: _safe_aggregate(block),
            finalize=_safe_finalize,
        )

    def get_target_column(self) -> Optional[str]:
        return self._target_col_name

    @abc.abstractmethod
    def combine(self, current_accumulator: AggType, new: AggType) -> AggType:
        """Combines new partially aggregated value (previously returned
        from `aggregate_block` partial aggregations into a singular partial
        aggregation) with the previously stored accumulator"""
        ...

    @abc.abstractmethod
    def aggregate_block(self, block: Block) -> AggType:
        """Applies aggregations to individual block (producing
        partial aggregation results)"""
        ...

    def _finalize(self, accumulator: AggType) -> Optional[U]:
        """Transforms partial aggregation into its final state (by default
        this is an identity transformation, ie no-op)"""
        return accumulator

    def _validate(self, schema: Optional["Schema"]) -> None:
        if self._target_col_name:
            from ray.data._internal.planner.exchange.sort_task_spec import SortKey

            SortKey(self._target_col_name).validate_schema(schema)


@PublicAPI
class Count(AggregateFnV2):
    """Defines count aggregation."""

    def __init__(
        self,
        on: Optional[str] = None,
        ignore_nulls: bool = False,
        alias_name: Optional[str] = None,
    ):
        super().__init__(
            alias_name if alias_name else f"count({on or ''})",
            on=on,
            ignore_nulls=ignore_nulls,
            zero_factory=lambda: 0,
        )

    def aggregate_block(self, block: Block) -> AggType:
        block_accessor = BlockAccessor.for_block(block)

        if self._target_col_name is None:
            # In case of global count, simply fetch number of rows
            return block_accessor.num_rows()

        return block_accessor.count(
            self._target_col_name, ignore_nulls=self._ignore_nulls
        )

    def combine(self, current_accumulator: AggType, new: AggType) -> AggType:
        return current_accumulator + new


@PublicAPI
class Sum(AggregateFnV2):
    """Defines sum aggregation."""

    def __init__(
        self,
        on: Optional[str] = None,
        ignore_nulls: bool = True,
        alias_name: Optional[str] = None,
    ):
        super().__init__(
            alias_name if alias_name else f"sum({str(on)})",
            on=on,
            ignore_nulls=ignore_nulls,
            zero_factory=lambda: 0,
        )

    def aggregate_block(self, block: Block) -> AggType:
        return BlockAccessor.for_block(block).sum(
            self._target_col_name, self._ignore_nulls
        )

    def combine(self, current_accumulator: AggType, new: AggType) -> AggType:
        return current_accumulator + new


@PublicAPI
class Min(AggregateFnV2):
    """Defines min aggregation."""

    def __init__(
        self,
        on: Optional[str] = None,
        ignore_nulls: bool = True,
        alias_name: Optional[str] = None,
    ):
        super().__init__(
            alias_name if alias_name else f"min({str(on)})",
            on=on,
            ignore_nulls=ignore_nulls,
            zero_factory=lambda: float("+inf"),
        )

    def aggregate_block(self, block: Block) -> AggType:
        return BlockAccessor.for_block(block).min(
            self._target_col_name, self._ignore_nulls
        )

    def combine(self, current_accumulator: AggType, new: AggType) -> AggType:
        return min(current_accumulator, new)


@PublicAPI
class Max(AggregateFnV2):
    """Defines max aggregation."""

    def __init__(
        self,
        on: Optional[str] = None,
        ignore_nulls: bool = True,
        alias_name: Optional[str] = None,
    ):

        super().__init__(
            alias_name if alias_name else f"max({str(on)})",
            on=on,
            ignore_nulls=ignore_nulls,
            zero_factory=lambda: float("-inf"),
        )

    def aggregate_block(self, block: Block) -> AggType:
        return BlockAccessor.for_block(block).max(
            self._target_col_name, self._ignore_nulls
        )

    def combine(self, current_accumulator: AggType, new: AggType) -> AggType:
        return max(current_accumulator, new)


@PublicAPI
class Mean(AggregateFnV2):
    """Defines mean aggregation."""

    def __init__(
        self,
        on: Optional[str] = None,
        ignore_nulls: bool = True,
        alias_name: Optional[str] = None,
    ):
        super().__init__(
            alias_name if alias_name else f"mean({str(on)})",
            on=on,
            ignore_nulls=ignore_nulls,
            # NOTE: We've to copy returned list here, as some
            #       aggregations might be modifying elements in-place
            zero_factory=lambda: list([0, 0]),  # noqa: C410
        )

    def aggregate_block(self, block: Block) -> AggType:
        block_acc = BlockAccessor.for_block(block)
        count = block_acc.count(self._target_col_name, self._ignore_nulls)

        if count == 0 or count is None:
            # Empty or all null.
            return None

        sum_ = block_acc.sum(self._target_col_name, self._ignore_nulls)

        if is_null(sum_):
            # In case of ignore_nulls=False and column containing 'null'
            # return as is (to prevent unnecessary type conversions, when, for ex,
            # using Pandas and returning None)
            return sum_

        return [sum_, count]

    def combine(self, current_accumulator: AggType, new: AggType) -> AggType:
        return [current_accumulator[0] + new[0], current_accumulator[1] + new[1]]

    def _finalize(self, accumulator: AggType) -> Optional[U]:
        if accumulator[1] == 0:
            return np.nan

        return accumulator[0] / accumulator[1]


@PublicAPI
class Std(AggregateFnV2):
    """Defines standard deviation aggregation.

    Uses Welford's online method for an accumulator-style computation of the
    standard deviation. This method was chosen due to its numerical
    stability, and it being computable in a single pass.
    This may give different (but more accurate) results than NumPy, Pandas,
    and sklearn, which use a less numerically stable two-pass algorithm.
    See
    https://en.wikipedia.org/wiki/Algorithms_for_calculating_variance#Welford's_online_algorithm
    """

    def __init__(
        self,
        on: Optional[str] = None,
        ddof: int = 1,
        ignore_nulls: bool = True,
        alias_name: Optional[str] = None,
    ):
        super().__init__(
            alias_name if alias_name else f"std({str(on)})",
            on=on,
            ignore_nulls=ignore_nulls,
            # NOTE: We've to copy returned list here, as some
            #       aggregations might be modifying elements in-place
            zero_factory=lambda: list([0, 0, 0]),  # noqa: C410
        )

        self._ddof = ddof

    def aggregate_block(self, block: Block) -> AggType:
        block_acc = BlockAccessor.for_block(block)
        count = block_acc.count(self._target_col_name, ignore_nulls=self._ignore_nulls)
        if count == 0 or count is None:
            # Empty or all null.
            return None
        sum_ = block_acc.sum(self._target_col_name, self._ignore_nulls)
        if is_null(sum_):
            # In case of ignore_nulls=False and column containing 'null'
            # return as is (to prevent unnecessary type conversions, when, for ex,
            # using Pandas and returning None)
            return sum_
        mean = sum_ / count
        M2 = block_acc.sum_of_squared_diffs_from_mean(
            self._target_col_name, self._ignore_nulls, mean
        )
        return [M2, mean, count]

    def combine(self, current_accumulator: List[float], new: List[float]) -> AggType:
        # Merges two accumulations into one.
        # See
        # https://en.wikipedia.org/wiki/Algorithms_for_calculating_variance#Parallel_algorithm
        M2_a, mean_a, count_a = current_accumulator
        M2_b, mean_b, count_b = new
        delta = mean_b - mean_a
        count = count_a + count_b
        # NOTE: We use this mean calculation since it's more numerically
        # stable than mean_a + delta * count_b / count, which actually
        # deviates from Pandas in the ~15th decimal place and causes our
        # exact comparison tests to fail.
        mean = (mean_a * count_a + mean_b * count_b) / count
        # Update the sum of squared differences.
        M2 = M2_a + M2_b + (delta**2) * count_a * count_b / count
        return [M2, mean, count]

    def _finalize(self, accumulator: List[float]) -> Optional[U]:
        # Compute the final standard deviation from the accumulated
        # sum of squared differences from current mean and the count.
        M2, mean, count = accumulator
        if count - self._ddof <= 0:
            return np.nan
        return math.sqrt(M2 / (count - self._ddof))


@PublicAPI
class AbsMax(AggregateFnV2):
    """Defines absolute max aggregation."""

    def __init__(
        self,
        on: Optional[str] = None,
        ignore_nulls: bool = True,
        alias_name: Optional[str] = None,
    ):
        if on is None or not isinstance(on, str):
            raise ValueError(f"Column to aggregate on has to be provided (got {on})")

        super().__init__(
            alias_name if alias_name else f"abs_max({str(on)})",
            on=on,
            ignore_nulls=ignore_nulls,
            zero_factory=lambda: 0,
        )

    def combine(self, current_accumulator: AggType, new: AggType) -> AggType:
        return max(current_accumulator, new)

    def aggregate_block(self, block: Block) -> AggType:
        block_accessor = BlockAccessor.for_block(block)

        max_ = block_accessor.max(self._target_col_name, self._ignore_nulls)
        min_ = block_accessor.min(self._target_col_name, self._ignore_nulls)

        if is_null(max_) or is_null(min_):
            return None

        return max(
            abs(max_),
            abs(min_),
        )


@PublicAPI
class Quantile(AggregateFnV2):
    """Defines Quantile aggregation."""

    def __init__(
        self,
        on: Optional[str] = None,
        q: float = 0.5,
        ignore_nulls: bool = True,
        alias_name: Optional[str] = None,
    ):
        self._q = q

        super().__init__(
            alias_name if alias_name else f"quantile({str(on)})",
            on=on,
            ignore_nulls=ignore_nulls,
            zero_factory=list,
        )

    def combine(self, current_accumulator: List[Any], new: List[Any]) -> List[Any]:
        if isinstance(current_accumulator, List) and isinstance(new, List):
            current_accumulator.extend(new)
            return current_accumulator

        if isinstance(current_accumulator, List) and (not isinstance(new, List)):
            if new is not None and new != "":
                current_accumulator.append(new)
            return current_accumulator

        if isinstance(new, List) and (not isinstance(current_accumulator, List)):
            if current_accumulator is not None and current_accumulator != "":
                new.append(current_accumulator)
            return new

        ls = []

        if current_accumulator is not None and current_accumulator != "":
            ls.append(current_accumulator)

        if new is not None and new != "":
            ls.append(new)

        return ls

    def aggregate_block(self, block: Block) -> AggType:
        block_acc = BlockAccessor.for_block(block)
        ls = []

        for row in block_acc.iter_rows(public_row_format=False):
            ls.append(row.get(self._target_col_name))

        return ls

    def _finalize(self, accumulator: List[Any]) -> Optional[U]:
        if self._ignore_nulls:
            accumulator = [v for v in accumulator if not is_null(v)]
        else:
            nulls = [v for v in accumulator if is_null(v)]
            if len(nulls) > 0:
                # NOTE: We return the null itself to preserve column type
                return nulls[0]

        if not accumulator:
            return None

        key = lambda x: x  # noqa: E731

        input_values = sorted(accumulator)
        k = (len(input_values) - 1) * self._q
        f = math.floor(k)
        c = math.ceil(k)

        if f == c:
            return key(input_values[int(k)])

        d0 = key(input_values[int(f)]) * (c - k)
        d1 = key(input_values[int(c)]) * (k - f)

        return round(d0 + d1, 5)


@PublicAPI
class Unique(AggregateFnV2):
    """Defines unique aggregation."""

    def __init__(
        self,
        on: Optional[str] = None,
        ignore_nulls: bool = True,
        alias_name: Optional[str] = None,
    ):
        super().__init__(
            alias_name if alias_name else f"unique({str(on)})",
            on=on,
            ignore_nulls=ignore_nulls,
            zero_factory=set,
        )

    def combine(self, current_accumulator: AggType, new: AggType) -> AggType:
        return self._to_set(current_accumulator) | self._to_set(new)

    def aggregate_block(self, block: Block) -> AggType:
        import pyarrow.compute as pac

        col = BlockAccessor.for_block(block).to_arrow().column(self._target_col_name)
        return pac.unique(col).to_pylist()

    @staticmethod
    def _to_set(x):
        if isinstance(x, set):
            return x
        elif isinstance(x, list):
            return set(x)
        else:
            return {x}


def _null_safe_zero_factory(zero_factory, ignore_nulls: bool):
    """NOTE: PLEASE READ CAREFULLY BEFORE CHANGING

    Null-safe zero factory is crucial for implementing proper aggregation
    protocol (monoid) w/o the need for additional containers.

    Main hurdle for implementing proper aggregation semantic is to be able to encode
    semantic of an "empty accumulator" and be able to tell it from the case when
    accumulator is actually holding null value:

        - Empty container can be overridden with any value
        - Container holding null can't be overridden if ignore_nulls=False

    However, it's possible for us to exploit asymmetry in cases of ignore_nulls being
    True or False:

        - Case of ignore_nulls=False entails that if there's any "null" in the sequence,
         aggregation is undefined and correspondingly expected to return null

        - Case of ignore_nulls=True in turn, entails that if aggregation returns "null"
        if and only if the sequence does NOT have any non-null value

    Therefore, we apply this difference in semantic to zero-factory to make sure that
    our aggregation protocol is adherent to that definition:

        - If ignore_nulls=True, zero-factory returns null, therefore encoding empty
        container
        - If ignore_nulls=False, couldn't return null as aggregation will incorrectly
        prioritize it, and instead it returns true zero value for the aggregation
        (ie 0 for count/sum, -inf for max, etc).
    """

    if ignore_nulls:

        def _safe_zero_factory(_):
            return None

    else:

        def _safe_zero_factory(_):
            return zero_factory()

    return _safe_zero_factory


def _null_safe_aggregate(
    aggregate: Callable[[Block], AggType],
    ignore_nulls: bool,
) -> Callable[[Block], Optional[AggType]]:
    def _safe_aggregate(block: Block) -> Optional[AggType]:
        result = aggregate(block)
        # NOTE: If `ignore_nulls=True`, aggregation will only be returning
        #       null if the block does NOT contain any non-null elements
        if is_null(result) and ignore_nulls:
            return None

        return result

    return _safe_aggregate


def _null_safe_finalize(
    finalize: Callable[[AggType], AggType]
) -> Callable[[Optional[AggType]], AggType]:
    def _safe_finalize(acc: Optional[AggType]) -> AggType:
        # If accumulator container is not null, finalize.
        # Otherwise, return as is.
        return acc if is_null(acc) else finalize(acc)

    return _safe_finalize


def _null_safe_combine(
    combine: Callable[[AggType, AggType], AggType], ignore_nulls: bool
) -> Callable[[Optional[AggType], Optional[AggType]], Optional[AggType]]:
    """Null-safe combination have to be an associative operation
    with an identity element (zero) or in other words implement a monoid.

    To achieve that in the presence of null values following semantic is
    established:

        - Case of ignore_nulls=True:
            - If current accumulator is null (ie empty), return new accumulator
            - If new accumulator is null (ie empty), return cur
            - Otherwise combine (current and new)

        - Case of ignore_nulls=False:
            - If new accumulator is null (ie has null in the sequence, b/c we're
            NOT ignoring nulls), return it
            - If current accumulator is null (ie had null in the prior sequence,
            b/c we're NOT ignoring nulls), return it
            - Otherwise combine (current and new)
    """

    if ignore_nulls:

        def _safe_combine(
            cur: Optional[AggType], new: Optional[AggType]
        ) -> Optional[AggType]:

            if is_null(cur):
                return new
            elif is_null(new):
                return cur
            else:
                return combine(cur, new)

    else:

        def _safe_combine(
            cur: Optional[AggType], new: Optional[AggType]
        ) -> Optional[AggType]:

            if is_null(new):
                return new
            elif is_null(cur):
                return cur
            else:
                return combine(cur, new)

    return _safe_combine
