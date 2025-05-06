# backend/services/retriever.py (where句修正 + ログ追加版)

import chromadb
from backend.services.embedding import get_embedding
import os
import traceback
import hashlib
import urllib.parse as _urlparse


# --------------------------------------------------------------------
# If CHROMA_POSTGRES_* variables are *not* individually supplied but a
# DATABASE_URL like
#   postgresql+psycopg2://user:password@host:port/dbname
# is present (as in production Secrets Manager), parse it and populate
# the individual CHROMA_POSTGRES_* environment variables so that
# chromadb's Settings() can pick them up.
# --------------------------------------------------------------------
_db_url = os.getenv("DATABASE_URL")
if _db_url and not os.getenv("CHROMA_POSTGRES_HOST"):
    _parsed = _urlparse.urlparse(_db_url)
    # scheme may be "postgres", "postgresql", "postgresql+psycopg2", etc.
    os.environ.setdefault("CHROMA_POSTGRES_HOST", _parsed.hostname or "")
    os.environ.setdefault("CHROMA_POSTGRES_PORT", str(_parsed.port or 5432))
    os.environ.setdefault("CHROMA_POSTGRES_USER", _parsed.username or "")
    os.environ.setdefault("CHROMA_POSTGRES_PASSWORD", _parsed.password or "")
    # path comes with a leading '/', strip it
    os.environ.setdefault("CHROMA_POSTGRES_DATABASE", (_parsed.path or "").lstrip("/"))

# --------------------------------------------------------------------
# ChromaDB client (PostgreSQL backend)
# --------------------------------------------------------------------
from chromadb import Client
from chromadb.config import Settings

#
# --------------------------------------------------------------------
# ChromaDB client (PostgreSQL backend)
#   全パラメータを環境変数から取得するよう統一
#   ・必須: CHROMA_DB_IMPL=postgres
#   ・CHROMA_POSTGRES_* は chromadb が自動取得
# --------------------------------------------------------------------
pg_settings = Settings(
    chroma_db_impl=os.getenv("CHROMA_DB_IMPL", "postgres"),
    anonymized_telemetry=False
)

print("[Retriever] Initializing ChromaDB client (Postgres backend)")
client = Client(pg_settings)
# --------------------------------------------------------------------


# コレクション取得または新規作成関数
def get_collection(user_id: int):
    """
    ユーザーごとに独立した Chroma Collection を取得/作成する。
    コレクション名: user_<user_id>_documents
    """
    name = f"user_{user_id}_documents"
    try:
        coll = client.get_or_create_collection(
            name=name,
            metadata={"hnsw:space": "cosine"}
        )
        return coll
    except Exception as e:
        print(f"CRITICAL: Failed to get/create collection '{name}': {e}")
        traceback.print_exc()
        return None

# ドキュメント追加関数 (変更なし)
def add_documents(chunks: list[str], source_name: str, user_id: int) -> bool:
    collection = get_collection(user_id)
    if collection is None:
        print("Error adding docs: ChromaDB collection unavailable.")
        return False
    if not chunks: print(f"No chunks for {source_name}"); return False
    if user_id is None: print("Error: user_id is required."); return False
    embeddings, ids, metadatas, documents_to_add = [], [], [], []
    for i, chunk in enumerate(chunks):
        if not chunk or not chunk.strip(): continue
        embedding = get_embedding(chunk)
        if embedding:
            embeddings.append(embedding)
            safe_source_name = "".join(c if c.isalnum() or c in ['-','_','.'] else '_' for c in source_name)
            hashed_id_part = hashlib.sha1(chunk.encode()).hexdigest()[:10]
            doc_id = f"user{user_id}_{safe_source_name[:40]}_{i}_{hashed_id_part}"
            ids.append(doc_id)
            metadatas.append({"source": source_name, "user_id": user_id}) # source名とuser_idをメタデータに
            documents_to_add.append(chunk)
    if not documents_to_add: print(f"No valid embeddings for {source_name}."); return False
    try:
        print(f"Upserting {len(documents_to_add)} docs for user {user_id}, source {source_name}...")
        collection.upsert(embeddings=embeddings, documents=documents_to_add, metadatas=metadatas, ids=ids)
        print(f"Success upsert for user {user_id}, source {source_name}")
        return True
    except Exception as e: print(f"Error upserting docs user {user_id}, source {source_name}: {e}"); traceback.print_exc(); return False

# 類似ドキュメント検索関数 (where句は単一条件なので変更なし)
def retrieve_similar_docs(query: str, user_id: int, top_k=3) -> dict:
    collection = get_collection(user_id)
    default_result = {"documents": [[]], "distances": [[]], "ids": [[]], "metadatas": [[]]}
    if collection is None: print("Error retrieving docs: ChromaDB unavailable."); return default_result
    if user_id is None: print("Error: user_id required."); return default_result
    query_embedding = get_embedding(query)
    if not query_embedding: return default_result
    try:
        # user_idでフィルタリング (単一条件なので $eq は必須ではないことが多い)
        results = collection.query(
            query_embeddings=[query_embedding],
            n_results=top_k,
            where={"user_id": user_id},
            include=['documents', 'distances', 'metadatas']
        )
        return results
    except Exception as e: print(f"Error querying Chroma user {user_id}: {e}"); traceback.print_exc(); return default_result

# 登録ソース一覧取得関数 (where句は単一条件なので変更なし)
def get_registered_sources(user_id: int) -> dict[str, int]:
    collection = get_collection(user_id)
    if collection is None: print("Error getting sources: ChromaDB unavailable."); return {}
    if user_id is None: print("Error: user_id required."); return {}
    sources_count: dict[str, int] = {}
    try:
        print(f"Getting sources for user {user_id}...")
        # user_idでフィルタリングしてメタデータを取得 (単一条件)
        results = collection.get(where={"user_id": user_id}, include=['metadatas'])
        if results and results.get('metadatas'):
            for metadata in results['metadatas']:
                if metadata and 'source' in metadata:
                    source_name = metadata.get('source');
                    if source_name: sources_count[source_name] = sources_count.get(source_name, 0) + 1
        print(f"Found sources for user {user_id}: {sources_count}")
        return sources_count
    except Exception as e: print(f"Error getting sources user {user_id}: {e}"); traceback.print_exc(); return {}

# ▼▼▼ get_documents_by_source の where句を修正 ▼▼▼
def get_documents_by_source(source_name: str, user_id: int, limit: int = 50) -> list[str]:
    """指定されたソース名とユーザーIDに一致するドキュメントの内容リストを取得"""
    collection = get_collection(user_id)
    if collection is None:
        print("[Retriever ERROR] get_documents_by_source: ChromaDB collection is unavailable.")
        return []
    if user_id is None:
        print("[Retriever ERROR] get_documents_by_source: user_id is required.")
        return []

    print(f"\n--- Retriever Function: get_documents_by_source ---")
    print(f"[Retriever] Attempting to get docs for user_id={user_id}, source='{source_name}', limit={limit}")

    try:
        # ★★★ メタデータでのフィルタリング条件を $and で結合 ★★★
        where_clause = {
            "$and": [
                {"source": {"$eq": source_name}}, # $eq (equals) 演算子を使用
                {"user_id": {"$eq": user_id}}    # $eq (equals) 演算子を使用
            ]
        }
        print(f"[Retriever] Using where clause for ChromaDB get(): {where_clause}")

        # ChromaDBに問い合わせ
        results = collection.get(
            where=where_clause,
            limit=limit,
            include=['documents'] # ドキュメント内容のみ取得
        )

        print(f"[Retriever] ChromaDB get() raw results: {results}")

        documents = results.get('documents', [])
        if not isinstance(documents, list):
            print(f"[Retriever WARNING] ChromaDB get() returned 'documents' but it's not a list: {type(documents)}")
            documents = []

        print(f"[Retriever] Extracted {len(documents)} documents from ChromaDB results.")
        return documents

    except Exception as e:
        print(f"[Retriever ERROR] Error in get_documents_by_source for user {user_id}, source '{source_name}': {e}")
        traceback.print_exc()
        return []
# ▲▲▲ get_documents_by_source の where句を修正 ▲▲▲


# ▼▼▼ delete_documents_by_source 内の get の where句も修正 ▼▼▼
def delete_documents_by_source(source_name: str, user_id: int) -> bool:
    """指定されたソース名とユーザーIDに一致するドキュメントを削除"""
    collection = get_collection(user_id)
    if collection is None: print("Error deleting docs: ChromaDB unavailable."); return False
    if user_id is None: print("Error: user_id required."); return False
    try:
        print(f"Attempting delete docs for user {user_id}, source: '{source_name}'...")

        # ★★★ 削除対象IDを取得するための where 句も $and で結合 ★★★
        where_clause_for_get = {
            "$and": [
                {"source": {"$eq": source_name}},
                {"user_id": {"$eq": user_id}}
            ]
        }
        print(f"[Retriever DELETE] Using where clause for initial get(): {where_clause_for_get}")

        ids_to_delete = collection.get(where=where_clause_for_get, include=[]).get('ids', [])
        if not ids_to_delete:
            print(f"No docs found for user {user_id}, source: '{source_name}'. Nothing to delete.");
            return True # 削除対象がなければ成功とみなす

        print(f"Deleting {len(ids_to_delete)} docs for user {user_id}, source: '{source_name}'...")
        # deleteメソッドはIDリストで指定するため、where句の修正は不要
        collection.delete(ids=ids_to_delete)

        # 削除確認 (削除後にもう一度getしてみる) - こちらの get の where も修正
        print(f"[Retriever DELETE] Verifying deletion using where clause: {where_clause_for_get}")
        remaining_docs = collection.get(where=where_clause_for_get, include=[]).get('ids', [])
        if not remaining_docs:
            print(f"Success delete user {user_id}, source: '{source_name}'");
            return True
        else:
            print(f"Warning: Deletion incomplete user {user_id}, source '{source_name}'. {len(remaining_docs)} remain.");
            return False
    except Exception as e:
        print(f"Error deleting docs user {user_id}, source '{source_name}': {e}");
        traceback.print_exc();
        return False