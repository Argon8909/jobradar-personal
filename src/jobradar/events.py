"""Run a synchronous event iterator and return its final value."""


def collect(iterator):
    while True:
        try:
            next(iterator)
        except StopIteration as completed:
            return completed.value
