from collections import MutableSet

class SetMixin(MutableSet):

    """
    Mix-in for sets.  You must define __iter__, add, remove
    """

    def __len__(self):
        return sum(1 for _ in self)

    def __contains__(self, item):
        return any(item == has_item for has_item in self)

    issubset = MutableSet.__le__
    issuperset = MutableSet.__ge__

    union = MutableSet.__or__
    intersection = MutableSet.__and__
    difference = MutableSet.__sub__
    symmetric_difference = MutableSet.__xor__

    def copy(self):
        return set(self)

    def update(self, other):
        self |= other

    def intersection_update(self, other):
        self &= other

    def difference_update(self, other):
        self -= other

    def symmetric_difference_update(self, other):
        self ^= other

    def discard(self, item):
        try:
            self.remove(item)
        except KeyError:
            pass

    @classmethod
    def _from_iterable(cls, it):
        return set(it)
