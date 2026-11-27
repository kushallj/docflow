from celery import shared_task

from .pipeline import process_document


@shared_task(name="documents.process", acks_late=True)
def process_document_task(document_id: int) -> str:
    return process_document(document_id).status
