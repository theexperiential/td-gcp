# Parameter Execute DAT -- watches the storage COMP (parent)
# Handles pulse buttons and reacts to key parameter changes.


def onPulse(par):
	ext = me.parent().ext.StorageExt
	name = par.name
	if name == 'Upload':
		ext.SyncFolder(direction='upload')
	elif name == 'Download':
		ext.SyncFolder(direction='download')
	elif name == 'Sync':
		ext.SyncFolder(direction='both')
	elif name == 'Connect':
		ext.Connect()
	elif name == 'Disconnect':
		ext.Disconnect()
	elif name == 'Reset':
		ext.Reset()
	elif name == 'Canceltransfers':
		ext.CancelTransfers()
	elif name == 'Createcallbacksdat':
		ext.CreateCallbacksDat()
	return


def onValueChange(par, previousValue):
	ext = me.parent().ext.StorageExt
	name = par.name

	# Private key set or changed -- reconnect (not reset) to preserve state
	if name == 'Privatekey':
		val = par.eval()
		if val and val != previousValue:
			ext._log('info', 'Private key changed -- reconnecting')
			ext.Disconnect()
			ext.Connect()
		return

	return


def onParExpressionChange(par, previousExpression):
	return


def onParModeChange(par, previousMode):
	return
