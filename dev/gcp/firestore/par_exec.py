# Parameter Execute DAT -- watches the firestore COMP (parent)
# Handles pulse buttons and reacts to key parameter changes.


def onPulse(par):
	ext = me.parent().ext.FirestoreExt
	name = par.name
	if name == 'Connect':
		ext.Connect()
	elif name == 'Disconnect':
		ext.Disconnect()
	elif name == 'Reset':
		ext.Reset()
	elif name == 'Flushqueue':
		ext.FlushWriteQueue()
	elif name == 'Flushdirty':
		ext.FlushDirty()
	elif name == 'Refreshcollections':
		ext.RefreshCollections()
	elif name == 'Createcallbacksdat':
		ext.CreateCallbacksDat()
	return


def onValueChange(par, previousValue):
	ext = me.parent().ext.FirestoreExt
	name = par.name

	# Private key set or changed -- reconnect (not reset) to preserve tables/queue
	if name == 'Privatekey':
		val = par.eval()
		if val and val != previousValue:
			ext._log('info', 'Private key changed -- reconnecting')
			ext.Disconnect()
			ext.Connect()
		return

	# Listener toggled on -- start watching collections if connected
	if name == 'Enablelistener':
		if par.eval():
			ext._log('info', 'Listener enabled -- starting watches')
			ext._discover_and_watch()
		else:
			ext._log('info', 'Listener disabled -- stopping watches')
			ext._stop_listeners()
		return

	# Collections changed while listener is active -- reconcile watches
	if name == 'Collections':
		if me.parent().par.Enablelistener.eval():
			new_colls = set(par.eval().split())
			# Treat * as blank (watch all)
			if new_colls == {'*'}:
				new_colls = set()
			# Use _active_collections as source of truth -- previousValue
			# is blank when in auto-discover mode, so the param doesn't
			# track what was actually watched.
			old_colls = set(ext._active_collections)

			if not new_colls:
				# Blank or * = watch all: unwatch old, then auto-discover
				for coll in old_colls:
					if coll:
						ext.UnwatchCollection(coll)
				ext._discover_and_watch()
			else:
				for coll in new_colls - old_colls:
					if coll:
						ext.WatchCollection(coll)
				for coll in old_colls - new_colls:
					if coll:
						ext.UnwatchCollection(coll)
		return

	return


def onParExpressionChange(par, previousExpression):
	return


def onParModeChange(par, previousMode):
	return