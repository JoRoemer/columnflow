# coding: utf-8

"""
Collection of general helpers and utilities.
"""

from __future__ import annotations

__all__ = [
    "get_root_processes_from_campaign", "add_shift_aliases", "get_shifts_from_sources",
    "create_category_id", "add_category", "create_category_combinations", "verify_config_processes",
]

import re
import itertools
from typing import Callable, Any

import law
import order as od


def get_root_processes_from_campaign(campaign: od.Campaign) -> od.UniqueObjectIndex:
    """
    Extracts all root process objects from datasets contained in an order campaign and returns them
    in a unique object index.
    """
    # get all dataset processes
    processes = set.union(*map(set, (dataset.processes for dataset in campaign.datasets)))

    # get their root processes
    root_processes = set.union(*map(set, (process.get_root_processes() for process in processes)))

    # create an empty index and fill subprocesses via walking
    index = od.UniqueObjectIndex(cls=od.Process)
    for root_process in root_processes:
        for process, _, _ in root_process.walk_processes(include_self=True):
            index.add(process, overwrite=True)

    return index


def add_shift_aliases(
    config: od.Config,
    shift_source: str,
    aliases: dict,
) -> None:
    """
    Extracts the two up and down shift instances from a *config* corresponding to a *shift_source*
    (i.e. the name of a shift without directions) and assigns *aliases* to their auxiliary data.

    Aliases should be given in a dictionary, mapping alias targets (keys) to sources (values). In
    both strings, template variables are injected with fields corresponding to all
    :py:class:`od.Shift` attributes, such as *name*, *id*, and *direction*.

    Example:

    .. code-block:: python

        add_shift_aliases(config, "pdf", {"pdf_weight": "pdf_weight_{direction}"})
        # adds {"pdf_weight": "pdf_weight_up"} to the "pdf_up" shift in "config"
        # plus {"pdf_weight": "pdf_weight_down"} to the "pdf_down" shift in "config"
    """
    for direction in ["up", "down"]:
        shift = config.get_shift(od.Shift.join_name(shift_source, direction))
        _aliases = shift.x("column_aliases", {})
        # format keys and values
        inject_shift = lambda s: re.sub(r"\{([^_])", r"{_\1", s).format(**shift.__dict__)
        _aliases.update({inject_shift(key): inject_shift(value) for key, value in aliases.items()})
        # extend existing or register new column aliases
        shift.x.column_aliases = _aliases


def get_shifts_from_sources(config: od.Config, *shift_sources: str) -> list[od.Shift]:
    """
    Takes a *config* object and returns a list of shift instances for both directions given a
    sequence *shift_sources*.
    """
    return sum(
        (
            [config.get_shift(f"{s}_up"), config.get_shift(f"{s}_down")]
            for s in shift_sources
        ),
        [],
    )


def create_category_id(
    config: od.Config,
    category_name: str,
    hash_len: int = 7,
    salt: Any = None,
) -> int:
    """
    Creates a unique id for a :py:class:`order.Category` named *category_name* in a
    :py:class:`order.Config` object *config* and returns it. Internally,
    :py:func:`law.util.create_hash` is used which receives *hash_len*. In case of an unintentional
    (yet unlikely) collision of two ids, there is the option to add a custom *salt* value.

    .. note::

        Please note that the size of the returned id depends on *hash_len*. When storing the id
        subsequently in an array, please be aware that values 8 or more require a ``np.int64``.
    """
    # create the hash
    h = law.util.create_hash((config.name, config.id, category_name, salt), l=hash_len)
    h = int(h, base=16)

    # add an offset to ensure that are hashes are above a threshold
    digits = len(str(int("F" * hash_len, base=16)))
    h += int(10 ** digits)

    return h


def add_category(config: od.Config, **kwargs) -> od.Category:
    """
    Creates a :py:class:`order.Category` instance by forwarding all *kwargs* to its constructor,
    adds it to a :py:class:`order.Config` object *config* and returns it. When *kwargs* do not
    contain a field *id*, :py:func:`create_category_id` is used to create one.
    """
    if "name" not in kwargs:
        fields = ",".join(map(str, kwargs))
        raise ValueError(f"a field 'name' is required to create a category, got '{fields}'")

    if "id" not in kwargs:
        kwargs["id"] = create_category_id(config, kwargs["name"])

    return config.add_category(**kwargs)


def create_category_combinations(
    config: od.Config,
    categories: dict[str, list[od.Categories]],
    name_fn: Callable[[Any], str],
    kwargs_fn: Callable[[Any], dict] | None = None,
    skip_existing: bool = True,
) -> int:
    """
    Given a *config* object and sequences of *categories* in a dict, creates all combinations of
    possible leaf categories at different depths, connects them with parent - child relations
    (see :py:class:`order.Category`) and returns the number of newly created categories.

    *categories* should be a dictionary that maps string names to sequences of categories that
    should be combined. The names are used as keyword arguments in a callable *name_fn* that is
    supposed to return the name of newly created categories (see example below).

    Each newly created category is instantiated with this name as well as arbitrary keyword
    arguments as returned by *kwargs_fn*. This function is called with the categories (in a
    dictionary, mapped to the sequence names as given in *categories*) that contribute to the newly
    created category and should return a dictionary. If the fields ``"id"`` and ``"selection"`` are
    missing, they are filled with reasonable defaults leading to a auto-generated, deterministic id
    and a list of all parent selection statements.

    If the name of a new category is already known to *config* it is skipped unless *skip_existing*
    is *False*.

    Example:

    .. code-block:: python

        categories = {
            "lepton": [cfg.get_category("e"), cfg.get_category("mu")],
            "n_jets": [cfg.get_category("1j"), cfg.get_category("2j")],
            "n_tags": [cfg.get_category("0t"), cfg.get_category("1t")],
        }

        def name_fn(lepton=None, n_jets=None, n_tags=None):
            # simple implementation: join names in defined order if existing
            parts = [lepton, n_jets, n_tags]
            return "__".join(part for part in parts if part is not None)

        def kwargs_fn(categories):
            # return arguments that are forwarded to the category init
            # (use id "+" here which simply increments the last taken id, see order.Category)
            # (note that this is also the default)
            return {"id": "+"}

        create_category_combinations(cfg, categories, name_fn, kwargs_fn)
    """
    n_created_categories = 0
    n_groups = len(categories)
    group_names = list(categories.keys())

    # nothing to do when there are less than 2 groups
    if n_groups < 2:
        return n_created_categories

    # check functions
    if not callable(name_fn):
        raise TypeError(f"name_fn must be a function, but got {name_fn}")
    if kwargs_fn and not callable(kwargs_fn):
        raise TypeError(f"when set, kwargs_fn must be a function, but got {kwargs_fn}")

    # start combining, considering one additional groups for combinatorics at a time
    for _n_groups in range(2, n_groups + 1):

        # build all group combinations
        for _group_names in itertools.combinations(group_names, _n_groups):

            # build the product of all categories for the given groups
            _categories = [categories[group_name] for group_name in _group_names]
            for root_cats in itertools.product(*_categories):
                # build the name
                root_cats = dict(zip(_group_names, root_cats))
                cat_name = name_fn(**{
                    group_name: cat.name
                    for group_name, cat in root_cats.items()
                })

                # skip when already existing
                if skip_existing and config.has_category(cat_name, deep=True):
                    continue

                # create arguments for the new category
                kwargs = kwargs_fn(root_cats) if callable(kwargs_fn) else {}
                if "id" not in kwargs:
                    kwargs["id"] = create_category_id(config, cat_name)
                if "selection" not in kwargs:
                    kwargs["selection"] = [c.selection for c in root_cats.values()]

                # create the new category
                cat = od.Category(name=cat_name, **kwargs)
                n_created_categories += 1

                # find direct parents and connect them
                for _parent_group_names in itertools.combinations(_group_names, _n_groups - 1):
                    if len(_parent_group_names) == 1:
                        parent_cat_name = root_cats[_parent_group_names[0]].name
                    else:
                        parent_cat_name = name_fn(**{
                            group_name: root_cats[group_name].name
                            for group_name in _parent_group_names
                        })
                    parent_cat = config.get_category(parent_cat_name, deep=True)
                    parent_cat.add_category(cat)

    return n_created_categories


def verify_config_processes(config: od.Config, warn: bool = False) -> None:
    """
    Verifies for all datasets contained in a *config* object that the linked processes are covered
    by any process object registered in *config* and raises an exception if not. If *warn* is
    *True*, a warning is printed instead.
    """
    missing_pairs = []
    for dataset in config.datasets:
        for process in dataset.processes:
            if not config.has_process(process):
                missing_pairs.append((dataset, process))

    # nothing to do when nothing is missing
    if not missing_pairs:
        return

    # build the message
    msg = f"found {len(missing_pairs)} dataset(s) whose process is not registered in the '{config.name}' config:"
    for dataset, process in missing_pairs:
        msg += f"\n  dataset '{dataset.name}' -> process '{process.name}'"

    # warn or raise
    if not warn:
        raise Exception(msg)

    print(f"{law.util.colored('WARNING', 'red')}: {msg}")
