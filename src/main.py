import asyncio
import os

from asyncio import Queue
from pathlib import Path

from config import settings
from src.extractors.labor_claim_calculation_extractor import (
    LaborClaimCalculationExtractor,
)
from utils.general_utils import get_logger

logger = get_logger(__name__)


PDF_PATH = Path(__file__).parents[1] / "data" / "Documentos"
NUM_CONSUMER_WORKERS = int((max(os.cpu_count() or 0, 1) / 2) - 2)
DEBUG = settings.DEBUG

queue: Queue = Queue()
extractor = LaborClaimCalculationExtractor()
extracted_data: dict[str, dict] = {}


async def produce_pdfs():
    """Coloca arquivos pdf em fila."""
    logger.info("[main] Starting to produce PDF paths...")

    pdfs = list(PDF_PATH.glob("*.pdf"))
    for pdf_path in pdfs:
        # pode ser verificação de jobs esperando para executar no banco, etc
        if pdf_path.name not in extracted_data:
            await queue.put(pdf_path)

    for _ in range(NUM_CONSUMER_WORKERS):
        await queue.put(None)

    logger.info("[main] Finished producing PDF paths...")


async def consume_worker():
    """Consome os pdfs da fila, e executa os workers com multi-threading"""
    logger.info("[main] Starting consumer worker...")
    while True:
        pdf = await queue.get()
        if pdf is None:
            queue.task_done()
            break

        result = await asyncio.to_thread(extractor.extract, pdf)
        # pode ser inserção no banco, etc
        extracted_data[pdf.name] = result
        queue.task_done()

    logger.info("[main] Finished consumer worker...")


async def main():
    """Executa producer e consumers e imprime os resultados extraídos."""

    logger.info("[main] Escalonando produtor e consumidor de PDF's")
    tasks = [asyncio.create_task(produce_pdfs())]
    tasks.extend(
        asyncio.create_task(consume_worker()) for _ in range(NUM_CONSUMER_WORKERS)
    )
    await asyncio.gather(*tasks)

    if DEBUG:
        for key, data in extracted_data.items():
            print(f"{key}: {data}")


if __name__ == "__main__":
    asyncio.run(main())
