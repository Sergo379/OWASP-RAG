"""Sample process RSS, including Ollama's external model runner (not just Python)."""

import threading

import psutil


class ResourceMonitor:
    def __init__(self):
        self.stop = threading.Event()
        self.peak_python = self.peak_ollama = self.peak_combined = 0
        self.min_available = psutil.virtual_memory().available
        self.thread = threading.Thread(target=self.run, daemon=True)

    def sample(self):
        python = psutil.Process().memory_info().rss
        ollama = 0
        processes = {}
        for process in psutil.process_iter(["name", "memory_info"]):
            try:
                if "ollama" in (process.info["name"] or "").lower():
                    processes[process.pid] = process
                    for child in process.children(recursive=True):
                        processes[child.pid] = child
            except (psutil.Error, TypeError, AttributeError):
                pass
        for process in processes.values():
            try:
                ollama += process.memory_info().rss
            except psutil.Error:
                pass
        self.peak_python = max(self.peak_python, python)
        self.peak_ollama = max(self.peak_ollama, ollama)
        self.peak_combined = max(self.peak_combined, python + ollama)
        self.min_available = min(self.min_available, psutil.virtual_memory().available)

    def run(self):
        while not self.stop.is_set():
            try:
                self.sample()
            except psutil.Error:
                pass
            self.stop.wait(0.25)

    def start(self):
        self.thread.start()

    def finish(self):
        self.stop.set()
        self.thread.join(timeout=1)
        return {
            "sample_interval_ms": 250,
            "peak_python_rss_bytes": self.peak_python,
            "peak_all_ollama_rss_bytes": self.peak_ollama,
            "peak_combined_rss_bytes": self.peak_combined,
            "minimum_system_available_bytes": self.min_available,
            "measurement": "Python + all Ollama processes and descendants; summed RSS may double-count shared pages",
        }
