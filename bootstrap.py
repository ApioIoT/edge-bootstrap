import sys
import subprocess
import time
import json
from pathlib import Path
import urllib.request
import urllib.parse
import hashlib
from enum import Enum
from datetime import datetime
from typing import Any, Optional, TypeVar, Callable
import shutil

class Retrier:
  RETRY_DELAY_SECONDS: int = 5
  MAX_RETRIES: int = 10

  T = TypeVar("T")
  def run(self, fn: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    last_exception = None

    for _ in range(self.MAX_RETRIES):
      try:
        return fn(*args, **kwargs)
      except Exception as e:
        last_exception = e
        print(e)
      
      time.sleep(self.RETRY_DELAY_SECONDS)
    
    if last_exception:
      raise last_exception

    raise Exception("Max retries reached")

class Os:
  def exec(self, cmd: str, printLog: bool = True) -> None:
    with subprocess.Popen(
      cmd,
      stdout=subprocess.PIPE,   # Reindirizza l'output standard
      stderr=subprocess.STDOUT, # Unisce l'errore standard allo standard output
      text=True,                # Restituisce stringhe (non byte)
      bufsize=1,                # Line buffering (per avere i dati riga per riga)
      shell=True
    ) as process:
      if printLog and process.stdout is not None:
        for row in process.stdout:
          print(row, end="")

    if process.returncode != 0:
      raise subprocess.CalledProcessError(process.returncode, cmd)
  
  def _isK3SInstalled(self) -> bool:
    return shutil.which("k3s") is not None

  def _installK3S(self) -> None:
    self.exec("curl -sfL https://get.k3s.io | sh -")

  def _isCurlInstalled(self) -> bool:
    return shutil.which("curl") is not None

  def _installCurl(self) -> None:
    self.exec("DEBIAN_FRONTEND=noninteractive apt-get update")
    self.exec("DEBIAN_FRONTEND=noninteractive apt-get -y install curl")

  def installK3S(self) -> None:
    if not self._isK3SInstalled():
      if not self._isCurlInstalled():
        self._installCurl()
      
      self._installK3S()

class Hardware:
  SERIAL_FILES: list[str] = [
    "/sys/class/dmi/id/product_serial",
    "/proc/device-tree/baseboard-sn",
    "/serial.txt" # TEST
  ]
  EXCLUDED_SERIALS: list[str] = [
    "",
    "Default string"
  ]

  def findDeviceSerial(self) -> str:
    for file in self.SERIAL_FILES:
      try:
        with open(file, "r", encoding="utf-8") as f:
          serial = f.read().strip()
          if serial not in self.EXCLUDED_SERIALS:
            return serial
      except FileNotFoundError:
        pass

    raise FileExistsError("device serial not found")

class Security:
  VOUCHER_FILE: str = "/voucher.txt"
  NONCE_FILE: str = "/nonce.txt"

  utility = Retrier()
  os = Os()
  
  def __init__(self, nonce: Optional[str] = None, voucher: Optional[str] = None) -> None:
    self._nonce = nonce
    self._voucher = voucher

  def _getVoucher(self) -> str:
    if self._voucher is not None:
      return self._voucher

    with open(self.VOUCHER_FILE, "r", encoding="utf-8") as f:
      return f.read().strip()

  def _getNonce(self) -> str:
    if self._nonce is not None:
      return self._nonce
    
    with open(self.NONCE_FILE, "r", encoding="utf-8") as f:
      return f.read().strip()

  def makeSecureVoucher(self) -> tuple[str, str]:
    nonce = self._getNonce()
    voucher = self._getVoucher()

    str = voucher + nonce
    hash = hashlib.sha1(str.encode('utf-8')).hexdigest()

    return hash, nonce
  
  def clean(self) -> None:
    if self._voucher is not None:
      self.os.exec(f"rm -f {self.VOUCHER_FILE}")
    
    if self._nonce is not None:
      self.os.exec(f"rm -f {self.NONCE_FILE}")

class Platform:
  PLATFORM_API_URI: str = "https://api.test.venera.apio.network"
  WAIT_DELAY_SECONDS: int = 5

  class Status(Enum):
    PROVISIONING_INSTALLING = 'provisioning_installing'
    PROVISIONING_JOIN = 'provisioning_join'
    PROVISIONING_CONFIGURING = 'provisioning_configuring'
    READY = 'ready'
    FAILED = 'failed'

  def setStatus(self, voucher: str, uuid: str, status: Status, on_status: Optional[Status] = None, reason: Optional[str] = None) -> None:
    try:
      url = urllib.parse.urljoin(self.PLATFORM_API_URI, f"/v1/enrollment/{uuid}?voucher={voucher}")

      payload: dict[str, Any] = {
        "status": status.value,
      }

      if on_status is not None:
        _reason = {
          "onStatus": on_status.value,
        }

        if reason is not None:
          _reason["msg"] = reason
        
        payload["reason"] = _reason

      json_payload = json.dumps(payload).encode("utf-8")

      req = urllib.request.Request(url, method="PUT", data=json_payload)
      with urllib.request.urlopen(req, timeout=2):
        pass
      
    except Exception as e:
      print(f"[Platform] Update status: {e}")

  def enroll(self, secureVoucher: str, nonce: str, deviceSerial: str) -> tuple[str, str]:
    url = urllib.parse.urljoin(self.PLATFORM_API_URI, "/v1/enroll")

    payload = {
      "nonce": nonce,
      "voucher": secureVoucher,
      "serial": deviceSerial,
    }
    json_payload = json.dumps(payload).encode("utf-8")

    req = urllib.request.Request(url, method="POST", data=json_payload)
    req.add_header("Content-Type", "application/json")

    with urllib.request.urlopen(req) as response:
      body = json.loads(response.read())
      return body["data"]["uuid"], body["data"]["url"]

  def waitForApprove(self, url: str) -> tuple[str, str]:
    while True:
      with urllib.request.urlopen(url) as response:
        body = json.loads(response.read())
        if body['data']['status'] == "approved":
          return body["data"]["data"]["management"]["url"], body["data"]["data"]["apikey"]["value"]
      
      time.sleep(self.WAIT_DELAY_SECONDS)

class K3S:
  WAIT_DEALY_SECONDS: int = 10
  MAX_WAIT_MINUTES: int = 20

  utility = Retrier()
  os = Os()

  def joinToRancher(self, uri: str) -> None:
    self.os.exec(f"curl --insecure -sfL {uri} | kubectl apply -f -")

  def setApiKeyOnPostgres(self, apiKey: str) -> None:
    self.os.exec(
      f'echo "INSERT INTO \\"apiKey\\" (value) VALUES (\'{apiKey}\');" | kubectl exec -i statefulset/postgres -n postgres-system -- su - postgres -c "psql -U username -d cloud-bridge"'
    )

  def waitForPostgresReady(self) -> None:
    start_time = datetime.now()

    while True:
      now = datetime.now()
      diff_minutes = int((now - start_time).total_seconds() / 60)
      if diff_minutes > self.MAX_WAIT_MINUTES:
        raise Exception("Time exceded")

      try:
        self.os.exec('''if [ "$(kubectl get statefulset postgres -n postgres-system -o jsonpath='{.status.readyReplicas}')" = "1" ]; then exit 0; else exit 1; fi''', False)
        return
      except Exception:
        print("...")
        time.sleep(self.WAIT_DEALY_SECONDS)

class Bootstrap:
  FLAG_FILE: Path = Path("/var/local/provisioning-done")

  retrier = Retrier()
  os = Os()
  hardware = Hardware()
  platform = Platform()
  k3s = K3S()

  def __init__(self, security: Security) -> None:
    self.security = security

  def isProvisioned(self) -> bool:
    return self.FLAG_FILE.exists()

  def markAsProvisioned(self) -> None:
    self.FLAG_FILE.touch()

  def run(self) -> None:
    print("[OS] Find device serial")
    serial = self.hardware.findDeviceSerial()

    secureVoucher, nonce = self.security.makeSecureVoucher()

    print("[Platform] Enroll")
    enrollmentUuid, enrollmentUri = self.retrier.run(self.platform.enroll, secureVoucher, nonce, serial)

    print("[Platform] Waiting for approve...")
    rancherUri, apiKey = self.retrier.run(self.platform.waitForApprove, enrollmentUri)
    
    try:
      print("[K3S] Install")
      self.platform.setStatus(secureVoucher, enrollmentUuid, Platform.Status.PROVISIONING_INSTALLING)
      self.retrier.run(self.os.installK3S)
    except Exception as e:
      self.platform.setStatus(secureVoucher, enrollmentUuid, Platform.Status.FAILED, Platform.Status.PROVISIONING_INSTALLING, str(e))
      raise e
    
    try:
      print("[K3S] Join to Rancher")
      self.platform.setStatus(secureVoucher, enrollmentUuid, Platform.Status.PROVISIONING_JOIN)
      self.retrier.run(self.k3s.joinToRancher, rancherUri)
    except Exception as e:
      self.platform.setStatus(secureVoucher, enrollmentUuid, Platform.Status.FAILED, Platform.Status.PROVISIONING_JOIN, str(e))
      raise e

    print("[K3S] Waiting for cluster ready...")
    self.k3s.waitForPostgresReady()
    
    try:
      self.platform.setStatus(secureVoucher, enrollmentUuid, Platform.Status.PROVISIONING_CONFIGURING)
      print("[K3S] Set ApiKey on Postgres")
      self.retrier.run(self.k3s.setApiKeyOnPostgres, apiKey)
    except Exception as e:
      self.platform.setStatus(secureVoucher, enrollmentUuid, Platform.Status.FAILED, Platform.Status.PROVISIONING_CONFIGURING, str(e))
      raise e
    
    self.platform.setStatus(secureVoucher, enrollmentUuid, Platform.Status.READY)

    self.security.clean()

    print("[V] Done")

def main() -> None:
  nonce = None
  if len(sys.argv) > 1:
    nonce = sys.argv[1]

  voucher = None
  if len(sys.argv) > 2:
    voucher = sys.argv[2]

  security = Security(nonce, voucher)
  bootstrap = Bootstrap(security)

  if not bootstrap.isProvisioned():
    try:
      bootstrap.run()
      bootstrap.markAsProvisioned()
    except KeyboardInterrupt:
      pass
    except Exception as e:
      print(f"[X] {e}")
      sys.exit(1)
  else:
    print("[V] device already provisioned")

if __name__ == "__main__":
  main()
