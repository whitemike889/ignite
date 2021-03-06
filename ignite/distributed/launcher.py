from typing import Callable, Optional

from ignite.distributed import utils as idist
from ignite.utils import setup_logger

__all__ = [
    "Parallel",
]


class Parallel:
    """Distributed launcher context manager to simplify distributed configuration setup for multiple backends:

    - backends from native torch distributed configuration: "nccl", "gloo", "mpi"

    - XLA on TPUs via `pytorch/xla <https://github.com/pytorch/xla>`_

    Namely, it can 1) spawn ``nproc_per_node`` child processes and initialize a processing group according to
    provided ``backend`` (useful for standalone scripts) or 2) only initialize a processing group given the ``backend``
    (useful with tools like `torch.distributed.launch`_).

    Examples:

        1) Single node or Multi-node, Multi-GPU training launched with `torch.distributed.launch`_ tool

        Single node option :

        .. code-block:: bash

            python -m torch.distributed.launch --nproc_per_node=4 --use_env main.py

        Multi-node option :

        .. code-block:: bash

            # node 0
            python -m torch.distributed.launch --nnodes=2 --node_rank=0 --master_addr=master \
                --master_port=3344 --nproc_per_node=8 --use_env main.py

            # node 1
            python -m torch.distributed.launch --nnodes=2 --node_rank=1 --master_addr=master \
                --master_port=3344 --nproc_per_node=8 --use_env main.py


        User code is the same for both options:

        .. code-block:: python

            # main.py

            import ignite.distributed as idist

            def training(local_rank, config, **kwargs):
                # ...
                print(idist.get_rank(), ": run with config:", config, "- backend=", idist.backend())
                # ...

            with idist.Parallel(backend="nccl") as parallel:
                parallel.run(training, config, a=1, b=2)


        2) Single node, Multi-GPU training launched with `python`

        .. code-block:: bash

            python main.py

        .. code-block:: python

            # main.py

            import ignite.distributed as idist

            def training(local_rank, config, **kwargs):
                # ...
                print(idist.get_rank(), ": run with config:", config, "- backend=", idist.backend())
                # ...

            with idist.Parallel(backend="nccl", nproc_per_node=4) as parallel:
                parallel.run(training, config, a=1, b=2)


        3) Single node, Multi-TPU training launched with `python`

        .. code-block:: bash

            python main.py

        .. code-block:: python

            # main.py

            import ignite.distributed as idist

            def training(local_rank, config, **kwargs):
                # ...
                print(idist.get_rank(), ": run with config:", config, "- backend=", idist.backend())
                # ...

            with idist.Parallel(backend="xla-tpu", nproc_per_node=8) as parallel:
                parallel.run(training, config, a=1, b=2)


        4) Multi-node, Multi-GPU training launched with `python`. For example, 2 nodes with 8 GPUs:

        .. code-block:: bash

            # node 0
            python main.py --node_rank=0

            # node 1
            python main.py --node_rank=1


        .. code-block:: python

            # main.py

            import ignite.distributed as idist

            def training(local_rank, config, **kwargs):
                # ...
                print(idist.get_rank(), ": run with config:", config, "- backend=", idist.backend())
                # ...

            dist_config = {
                "nproc_per_node": 8,
                "nnodes": 2,
                "node_rank": args.node_rank,
                "master_addr": "master",
                "master_port": 15000
            }

            with idist.Parallel(backend="nccl", **dist_config) as parallel:
                parallel.run(training, config, a=1, b=2)

    .. _torch.distributed.launch: https://pytorch.org/docs/stable/distributed.html#launch-utility

    Args:
        backend (str, optional): backend to use: `nccl`, `gloo`, `xla-tpu`. If None, no distributed configuration.
        nproc_per_node (int, optional): optional argument, number of processes per
            node to specify. If not None, :meth:`~ignite.distributed.Parallel.run` will spawn ``nproc_per_node``
            processes that run input function with its arguments.
        nnodes (int, optional): optional argument, number of nodes participating in distributed configuration.
            If not None, :meth:`~ignite.distributed.Parallel.run` will spawn ``nproc_per_node``
            processes that run input function with its arguments. Total world size is `nproc_per_node * nnodes`.
        node_rank (int, optional): optional argument, current machine index. Mandatory argument if ``nnodes`` is
            specified and larger than one.
        master_addr (str, optional): optional argument, master node TCP/IP address for torch native backends
            (`nccl`, `gloo`). Mandatory argument if ``nnodes`` is specified and larger than one.
        master_port (int, optional): optional argument, master node port for torch native backends
            (`nccl`, `gloo`). Mandatory argument if ``master_addr`` is specified.
        **spawn_kwargs: kwargs to ``idist.spawn`` function.
    """

    def __init__(
        self,
        backend: str = None,
        nproc_per_node: Optional[int] = None,
        nnodes: Optional[int] = None,
        node_rank: Optional[int] = None,
        master_addr: Optional[str] = None,
        master_port: Optional[str] = None,
        **spawn_kwargs
    ):
        if backend is not None:
            if backend not in idist.available_backends():
                raise ValueError(
                    "Unknown backend '{}'. Available backends: {}".format(backend, idist.available_backends())
                )
        else:
            arg_names = ["nproc_per_node", "nnodes", "node_rank", "master_addr", "master_port"]
            arg_values = [nproc_per_node, nnodes, node_rank, master_addr, master_port]
            for name, value in zip(arg_names, arg_values):
                if value is not None:
                    raise ValueError(
                        "If backend is None, argument '{}' should be also None, but given {}".format(name, value)
                    )

        self.backend = backend
        self._spawn_params = None
        self.logger = setup_logger(__name__ + "." + self.__class__.__name__, distributed_rank=0)
        # distributed_rank=0 <=> explicit rank 0, avoid call idist. Critical for TPU on Colab, avoid context is setup

        if self.backend is not None:
            if nproc_per_node is not None:
                self._spawn_params = self._setup_spawn_params(
                    nproc_per_node, nnodes, node_rank, master_addr, master_port, **spawn_kwargs
                )

        if self._spawn_params is not None:
            self.logger.info("Initialized distributed launcher with backend: '{}'".format(self.backend))
            msg = "\n\t".join(["{}: {}".format(k, v) for k, v in self._spawn_params.items() if v is not None])
            self.logger.info("- Parameters to spawn processes: \n\t{}".format(msg))

    @staticmethod
    def _setup_spawn_params(nproc_per_node, nnodes, node_rank, master_addr, master_port, **spawn_kwargs):
        if nproc_per_node < 1:
            raise ValueError("Argument nproc_per_node should positive, but given {}".format(nproc_per_node))
        if nnodes is None:
            nnodes = 1
        if nnodes < 1:
            raise ValueError("Argument nnodes should positive, but given {}".format(nnodes))
        if node_rank is None:
            if nnodes > 1:
                raise ValueError("If number of nodes larger than one, arguments node_rank should be given")
            node_rank = 0
        if node_rank >= nnodes or node_rank < 0:
            raise ValueError(
                "Argument node_rank should be between 0 and {}, but given {}".format(nnodes - 1, node_rank)
            )
        if nnodes > 1 and (master_addr is None or master_port is None):
            raise ValueError(
                "If number of nodes larger than one, arguments master_addr and master_port "
                "should be specified, but given master_addr={} and master_port={}".format(master_addr, master_port)
            )
        params = {
            "nproc_per_node": nproc_per_node,
            "nnodes": nnodes,
            "node_rank": node_rank,
            "master_addr": master_addr,
            "master_port": master_port,
        }
        params.update(spawn_kwargs)
        return {k: v for k, v in params.items() if v is not None}

    def run(self, func: Callable, *args, **kwargs):
        """Execute ``func`` with provided arguments in distributed context.

        Example

        .. code-block:: python

            def training(local_rank, config, **kwargs):
                # ...
                print(idist.get_rank(), ": run with config:", config, "- backend=", idist.backend())
                # ...

        Args:
            func (Callable): function to execute. First argument of the function should be `local_rank` - local process
                index.
            *args: positional arguments of ``func`` (without `local_rank`).
            **kwargs: keyword arguments of ``func``.

        """
        if self._spawn_params is not None:
            self.logger.info("Spawn function '{}' in {} processes".format(func, self._spawn_params["nproc_per_node"]))
            idist.spawn(self.backend, func, args=args, kwargs_dict=kwargs, **self._spawn_params)
        else:
            self.logger.info("- Run '{}' in {} processes".format(func, idist.get_world_size()))
            local_rank = idist.get_local_rank()
            func(local_rank, *args, **kwargs)

        self.logger.info("End of run")

    def __enter__(self):
        if (self.backend is not None) and self._spawn_params is None:
            idist.initialize(self.backend)
            self.logger = setup_logger(__name__ + "." + self.__class__.__name__)
            self.logger.info("Initialized processing group with backend: '{}'".format(self.backend))

        return self

    def __exit__(self, *args, **kwargs):
        if (self.backend is not None) and self._spawn_params is None:
            self.logger.info("Finalized processing group with backend: '{}'".format(self.backend))
            idist.finalize()
