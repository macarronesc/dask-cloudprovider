import dask
from dask_cloudprovider.config import ClusterConfig
import dask.config

from dask_cloudprovider.generic.vmcluster import (
    VMCluster,
    VMInterface,
    SchedulerMixin,
    WorkerMixin,
)
import time
from distributed.core import Status

try:
    from ibm_code_engine_sdk.code_engine_v2 import CodeEngineV2
    from ibm_cloud_sdk_core.authenticators import IAMAuthenticator
    from ibm_code_engine_sdk.ibm_cloud_code_engine_v1 import IbmCloudCodeEngineV1
except ImportError as e:
    msg = (
        "Dask Cloud Provider IBM requirements are not installed.\n\n"
        "Please either conda or pip install as follows:\n\n"
        "  conda install -c conda-forge dask-cloudprovider       # either conda install\n"
        '  pip install "dask-cloudprovider[ibm]" --upgrade       # or python -m pip install'
    )
    raise ImportError(msg) from e


class IBMCodeEngine(VMInterface):
    def __init__(
        self,
        cluster: str,
        config,
        image: str = None,
        region: str = None,
        project_id: str = None,
        api_key: str = None,
        *args,
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        self.cluster = cluster
        self.config = config
        self.image = image
        self.region = region
        self.project_id = project_id
        self.api_key = api_key
        
        authenticator = IAMAuthenticator(self.api_key, url='https://iam.cloud.ibm.com')
        authenticator.set_disable_ssl_verification(True)  # Disable SSL verification for the authenticator

        self.code_engine_service = CodeEngineV2(authenticator=authenticator)
        self.code_engine_service.set_service_url('https://api.' + self.region + '.codeengine.cloud.ibm.com/v2')
        self.code_engine_service.set_disable_ssl_verification(True)  # Disable SSL verification for the service instance

    async def create_vm(self):
        response = self.code_engine_service.create_app(
            project_id=self.project_id,
            image_reference=self.image,
            name='my-app',
            run_commands=['python', '-m', 'distributed.cli.dask_scheduler'],
            image_port=8786,
        )
        app = response.get_result()

        while True:
            response = self.code_engine_service.get_app(
                project_id=self.project_id,
                name='my-app',
            )
            app = response.get_result()
            if app["status"] == "ready":
                break
            
            time.sleep(1)

        internal_url = app["endpoint_internal"].split("//")[1]
        external_url = app["endpoint"].split("//")[1]

        return internal_url, external_url

    async def destroy_vm(self):
        response = self.code_engine_service.delete_app(
            project_id=self.project_id,
            name='my-app',
        )


# TODO: Try this new way (use url instead of ip and port)
# TODO: Create and use a tls certificate
class IBMCodeEngineScheduler(SchedulerMixin, IBMCodeEngine):
    """Scheduler running in a GCP instance."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

    async def start(self):
        await self.start_scheduler()
        self.status = Status.running

    async def start_scheduler(self):
        self.cluster._log(
            f"Launching cluster with the following configuration: "
            f"\n  Source Image: {self.image} "
            f"\n  Region: {self.region} "
            f"\n  Project id: {self.project_id} "
        )
        self.cluster._log("Creating scheduler instance")
        self.internal_ip, self.external_ip = await self.create_vm()
        self.address = f"{self.cluster.protocol}://{self.external_ip}:443"
        await self.wait_for_scheduler()

        # need to reserve internal IP for workers
        # gcp docker containers can't see resolve ip address
        self.cluster.scheduler_internal_ip = self.internal_ip
        self.cluster.scheduler_external_ip = self.external_ip
        self.cluster.scheduler_port = self.port


class IBMCodeEngineWorker(WorkerMixin, IBMCodeEngine):
    pass


class IBMCodeEngineCluster(VMCluster):
    def __init__(
        self,
        image: str = None,
        region: str = None,
        project_id: str = None,
        debug: bool = False,
        **kwargs,
    ):
        self.config = ClusterConfig(dask.config.get("cloudprovider.ibm", {}))
        self.scheduler_class = IBMCodeEngineScheduler
        self.worker_class = IBMCodeEngineWorker
        
        self.image = image or self.config.get("image")
        self.region = region or self.config.get("region")
        self.project_id = project_id or self.config.get("project_id")
        api_key = self.config.get("api_key")

        self.debug = debug
        
        self.options = {
            "cluster": self,
            "config": self.config,
            "image": self.image,
            "region": self.region,
            "project_id": self.project_id,
            "api_key": api_key,
        }
        self.scheduler_options = {**self.options}
        self.worker_options = {**self.options}
        super().__init__(debug=debug, **kwargs)
