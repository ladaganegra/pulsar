from time import time

from pulsar import create_connection
from pulsar.utils.py2py3 import iteritems
from pulsar.utils.tools import gen_unique_id

from .defer import Deferred, is_async

__all__ = ['ActorMessage',
           'ActorProxy',
           'ActorProxyMonitor',
           'get_proxy',
           'ActorCallBack',
           'ActorCallBacks',
           'DEFAULT_MESSAGE_CHANNEL']


DEFAULT_MESSAGE_CHANNEL = '__message__'       
        


def get_proxy(obj, safe = False):
    if isinstance(obj,ActorProxy):
        return obj
    elif hasattr(obj,'proxy'):
        return get_proxy(obj.proxy)
    else:
        if safe:
            return None
        else:
            raise ValueError('"{0}" is not a remote or remote proxy.'.format(obj))

        
def actorid(actor):
    return actor.aid if hasattr(actor,'aid') else actor


class ActorCallBack(Deferred):
    '''An actor callback run on the actor event loop'''
    def __init__(self, actor, request, *args, **kwargs):
        super(ActorCallBack,self).__init__()
        self.args = args
        self.kwargs = kwargs
        self.actor = actor
        self.request = request
        if is_async(request):
            self()
        else:
            self.callback(self.request)
        
    def __call__(self):
        if self.request.called:
            self.callback(self.request.result)
        else:
            self.actor.ioloop.add_callback(self)


class ActorCallBacks(Deferred):
    
    def __init__(self, actor, requests):
        super(ActorCallBacks,self).__init__()
        self.actor = actor
        self.requests = []
        self._tmp_results = []
        for r in requests:
            if is_async(r):
                self.requests.append(r)
            else:
                self._tmp_results.append(r)
        actor.ioloop.add_callback(self)
        
    def __call__(self):
        if self.requests:
            nr = []
            for r in self.requests:
                if r.called:
                    self._tmp_results.append(r.result)
                else:
                    nr.append(r)
            self.requests = nr
        if self.requests:
            self.actor.ioloop.add_callback(self)
        else:
            self.callback(self._tmp_results)
            

class CallerCallBack(object):
    __slots__ = ('rid','proxy','caller')
    
    def __init__(self, request, actor, caller):
        self.rid = request.rid
        self.proxy = actor.proxy
        self.caller = caller
        
    def __call__(self, result):
        self.proxy.callback(self.caller,self.rid,result)
        

class ActorMessage(Deferred):
    '''A message class which encapsulate the logic for sending and receiving
messages.'''
    REQUESTS = {}
    
    def __init__(self, sender, target, action, ack, msg):
        super(ActorMessage,self).__init__(rid = gen_unique_id()[:8])
        self.sender = actorid(sender)
        self.receiver = actorid(target)
        self.action = action
        self.msg = msg
        self.ack = ack
        if self.ack:
            self.REQUESTS[self.rid] = self
        
    def __str__(self):
        return '[0] - {1} {2} {3}'.format(self.rid,self.sender,self.action,
                                          self.receiver)
    
    def __repr__(self):
        return self.__str__()
    
    def __getstate__(self):
        #Remove the list of callbacks and lock
        d = self.__dict__.copy()
        d.pop('_lock',None)
        d['_callbacks'] = []
        return d
    
    def make_actor_callback(self, actor, caller):
        return CallerCallBack(self, actor, caller)
            
    @classmethod
    def actor_callback(cls, rid, result):
        r = cls.REQUESTS.pop(rid,None)
        if r:
            r.callback(result)
            

class ActorProxyRequest(object):
    '''A class holding information about a message to be sent from one
actor to another'''
    __slots__ = ('caller','_func_name','ack')
    
    def __init__(self, caller, name, ack):
        self.caller = caller
        self._func_name = name
        self.ack = ack
        
    def __repr__(self):
        return '{0} calling "{1}"'.format(self.caller,self._func_name)
        
    def __call__(self, *args, **kwargs):
        if len(args) == 0:
            actor = self.caller
        else:
            actor = args[0]
            args = args[1:]
        ser = self.serialize
        args = tuple((ser(a) for a in args))
        kwargs = dict((k,ser(a)) for k,a in iteritems(kwargs))
        actor = get_proxy(actor)
        return actor.send(self.caller.aid,(args,kwargs), name = self._func_name,
                          ack = self.ack)
    
    def serialize(self, obj):
        if hasattr(obj,'aid'):
            return obj.aid
        else:
            return obj


class ActorProxy(object):
    '''This is an important component in pulsar concurrent framework. An
instance of this class behaves as a proxy for a remote `underlying` 
:class:`Actor` instance.
This is a lightweight class which delegates function calls to the underlying
remote object.

It is pickable and therefore can be send from actor to actor using pulsar
messaging.

A proxy exposes all the underlying remote functions which have been implemented
in the actor class by prefixing with ``actor_``
(see the :class:`pulsar.ActorMetaClass` documentation).

By default each actor comes with a set of remote functions:

 * ``info`` returns a dictionary of information about the actor
 * ``ping`` returns ``pong``.
 * ``notify``
 * ``stop`` stop the actor
 * ``on_actor_exit``
 * ``callback``

For example, lets say we have a proxy ``a`` and an actor (or proxy) ``b``::

    a.send(b,'notify','hello there!')
    
will call ``notify`` on the actor ``a`` with ``b`` as the sender.
    

.. attribute:: proxyid

    Unique ID for the remote object
    
.. attribute:: remotes

    dictionary of remote functions names with value indicating if the
    remote function will acknowledge the call or not.
    
.. attribute:: timeout

    the value of the underlying :attr:`pulsar.Actor.timeout` attribute
'''     
    def __init__(self, impl):
        self.aid = impl.aid
        self.remotes = impl.remotes
        self.mailbox = impl.inbox
        self.timeout = impl.timeout
        self.loglevel = impl.loglevel
        
    def send(self, sender, action, *args, **kwargs):
        '''\
Send a message to the underlying actor (the receiver). This is the low level
function call for communicating between actors.

:parameter sender: the actor sending the message.
:parameter action: the action of the message. If not provided,
    the message will be broadcasted by the receiving actor,
    otherwise a specific action will be performed.
    Default ``None``.
:parameter args: the message body.
:parameter ack: If ``True`` the receiving actor will send a callback.
    If the action is provided and available, this parameter will be overritten.
:rtype: an instance of :class:`ActorRequest`.

When sending a message, first we check the ``sender`` outbox. If that is
not available, we get the receiver ``inbox`` and hope it can carry the message.
If there is no inbox either, abort the message passing and log a critical error.
    '''
        mailbox = sender.outbox
        # if the sender has no outbox, pick the receiver mailbox an hope
        # for the best
        if not mailbox:
            mailbox = self.mailbox
        
        if not mailbox:
            sender.log.critical('Cannot send a message to {0}. There is no\
 mailbox available.'.format(self))
            return
        
        ack = False
        if action in self.remotes:
            ack = self.remotes[action]
        request = ActorMessage(sender,self.aid,action,ack,(args,kwargs))
        try:
            mailbox.put(request)
            return request
        except Exception as e:
            sender.log.error('Failed to send message {0}: {1}'.\
                            format(request,e), exc_info = True)
        
    def __repr__(self):
        return self.aid[:8]
    
    def __str__(self):
        return self.__repr__()
    
    def __eq__(self, o):
        o = get_proxy(o,True)
        return o and self.aid == o.aid
    
    def __ne__(self, o):
        return not self.__eq__(o) 
    
    def __getstate__(self):
        '''Because of the __getattr__ implementation,
we need to manually implement the pickling and unpickling of the object.'''
        return (self.aid,self.remotes,self.mailbox,self.timeout,self.loglevel)
    
    def __setstate__(self, state):
        self.aid,self.remotes,self.mailbox,self.timeout,self.loglevel = state
        
    def get_request(self, action):
        if action in self.remotes:
            ack = self.remotes[action]
            return ActorProxyRequest(self, action, ack)
        else:
            raise AttributeError("'{0}' object has no attribute '{1}'"\
                                 .format(self,action))

    def __getattr__(self, name):
        return self.get_request(name)

    def local_info(self):
        '''Return a dictionary containing information about the remote
object including, aid (actor id), timeout and mailbox size.'''
        return {'aid':self.aid[:8],
                'timeout':self.timeout,
                'mailbox_size':self.mailbox.qsize()}


class ActorProxyMonitor(ActorProxy):
    '''A specialized :class:`pulsar.ActorProxy` class which contains additional
information about the remote underlying :class:`pulsar.Actor`. Unlike the
:class:`pulsar.ActorProxy` class, instances of this class are not pickable and
therefore remain in the process where they have been created.'''
    def __init__(self, impl):
        self.impl = impl
        self.info = {'last_notified':time()}
        self.stopping = 0
        super(ActorProxyMonitor,self).__init__(impl)
    
    @property
    def notified(self):
        return self.info['last_notified']
    
    @property
    def pid(self):
        return self.impl.pid
    
    def is_alive(self):
        '''True if underlying actor is alive'''
        return self.impl.is_alive()
        
    def terminate(self):
        '''Terminate life of underlying actor.'''
        self.impl.terminate()
            
    def __str__(self):
        return self.impl.__str__()
    
    def local_info(self):
        '''Return a dictionary containing information about the remote
object including, aid (actor id), timeout mailbox size, last notified time and
process id.'''
        return self.info
