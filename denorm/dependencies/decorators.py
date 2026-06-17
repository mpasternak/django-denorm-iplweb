from .callback import CallbackDependOnRelated
from .onfields import DependOnFields


def make_depend_decorator(Class):
    """
    Create a decorator that attaches an instance of the given class
    to the decorated function, passing all remaining arguments to the classes
    __init__.
    """
    import functools

    def decorator(*args, **kwargs):
        def deco(func):
            if not hasattr(func, "depend"):
                func.depend = []

            # Pass function name to CallbackDependOnRelated
            if "func" in kwargs:
                raise NameError(
                    "Argument 'func' is restricted, please use different one."
                )
            kwargs["func"] = func

            func.depend.append((Class, args, kwargs))
            return func

        return deco

    functools.update_wrapper(decorator, Class.__init__)
    return decorator


depend_on_related = make_depend_decorator(CallbackDependOnRelated)
depend_on_fields = make_depend_decorator(DependOnFields)
