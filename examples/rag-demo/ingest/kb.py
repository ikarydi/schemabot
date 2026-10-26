"""Embed knowledge-base articles with the OpenAI embeddings API."""

from openai import OpenAI


def make_client():
    return OpenAI()


def embed_articles(client, articles):
    texts = [f"{a.title}\n{a.content}" for a in articles]
    response = client.embeddings.create(model="text-embedding-3-small", input=texts)
    return [
        {"id": a.id, "workspace_id": a.workspace_id, "vector": item.embedding}
        for a, item in zip(articles, response.data)
    ]
