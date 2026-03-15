# Recipes

Short, practical examples for common use cases.

## Live Dashboard

Connect Firestore data to TouchDesigner visuals. Each document in a `metrics` collection drives a text display.

```python
# In a callbacks DAT:
def onFirestoreChange(collection, doc_id, change_type, payload):
    if collection == 'metrics' and change_type != 'removed':
        # Update a text TOP with the latest value
        text_op = op(f'display_{doc_id}')
        if text_op:
            text_op.par.text = f"{payload.get('label', doc_id)}: {payload.get('value', '')}"
```

Reference individual cells directly in parameter expressions:

```
# On a Text TOP's "text" parameter:
op.firestore.op('collections/metrics')[1, 'payload']
```

## Remote Control

Use a Firestore `controls` collection to remotely trigger actions in TouchDesigner.

```python
def onFirestoreChange(collection, doc_id, change_type, payload):
    if collection != 'controls':
        return

    if doc_id == 'scene' and change_type == 'modified':
        scene_name = payload.get('active', '')
        if scene_name:
            op('scene_switcher').par.index = int(payload.get('index', 0))

    if doc_id == 'command' and payload.get('action') == 'reset':
        op('main_comp').par.reinitextensions.pulse()
        # Clear the command so it doesn't re-trigger
        op.firestore.PushDoc('controls', 'command', {'action': ''})
```

## Batch Upload from a Table

Seed a Firestore collection from an existing TouchDesigner table.

```python
# Read from a tableDAT and push each row as a document
source = op('my_data_table')
ops = []
for row in range(1, source.numRows):
    doc_id = source[row, 'id'].val
    ops.append({
        'collection': 'catalog',
        'doc_id': doc_id,
        'op_type': 'set',
        'payload': {
            'name': source[row, 'name'].val,
            'price': float(source[row, 'price'].val),
            'category': source[row, 'category'].val,
        }
    })

op.firestore.PushBatch(ops)
```

## Offline-First Installation

Configure the COMP so that your project works without internet, syncing when connectivity returns.

1. Set **Enable Cache** to On
2. Set **Hydrate on Start** to On
3. Run the project once with internet to populate the cache

On subsequent launches without internet:

- Bootstrap skips (venv already exists)
- Connect fails, circuit breaker opens
- Hydration populates tableDATs from the SQLite cache
- Any local writes are queued to SQLite
- When internet returns and the circuit half-opens, everything syncs

## Multiple Firestore Databases

GCP supports [named databases](https://firebase.google.com/docs/firestore/manage-databases) in addition to the default. To connect to a named database, set the **Database ID** parameter:

```
Database ID: my-analytics-db
```

To use multiple databases simultaneously, add multiple Firestore COMPs to your project -- each with a different Database ID and the same (or different) service account key.
