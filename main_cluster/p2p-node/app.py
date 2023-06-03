import hashlib
from flask import Flask, request, jsonify, session, g
from flask_apscheduler import APScheduler
from apscheduler.schedulers.background import BackgroundScheduler
from pymongo import MongoClient
from bson.objectid import ObjectId
from collections import defaultdict
import requests, socket, logging, time, sys, threading,os,hashlib
import random


# CONSTANTS
RETRY_COUNT = 3

'''
GLOBAL VARIABLES
when using global variables, use the global_var dictionary
'''
global_var = defaultdict(lambda: None)

# creates app
app = Flask(__name__)

# scheduler
scheduler = APScheduler()

'''
PRINT STATEMENTS DO NOT WORK
since print() statements will not be visible outside of if __name__ == "__main__":

INSTEAD USE THE FOLLOWING
the following can be used to output debug messages to kubectl logs: 
# app.logger.debug("message")
'''
# following line makes debug messages visible in kubectl logs
app.logger.setLevel(logging.DEBUG)

# SQL database
mongo_client = MongoClient('mongodb://localhost:27017')
db = mongo_client.my_database

users_db = db['users']

leaders_db = db['leaders']
subscribed_topics_db = db['subscribed_topics']
topic_neighbors_db = db['topic_neighbors']
posts_db = db['posts']

# lock mechanism
class ReadAndWriteLock():
    def __init__(self):
        self.write_lock = threading.Lock()
        self.readers_lock = threading.Lock()
        self.readers_count = 0
    
    def acquire_writelock(self):
        self.write_lock.acquire()

    def release_writelock(self):
        self.write_lock.release()

    def acquire_readlock(self):
        self.readers_lock.acquire()
        self.readers_count += 1
        if self.readers_count == 1:
            self.write_lock.acquire()
        self.readers_lock.release()

    def release_readlock(self):
        self.readers_lock.acquire()
        self.readers_count -= 1
        if self.readers_count == 0:
            self.write_lock.release()
        self.readers_lock.release()
    
rw_locks = defaultdict(lambda:ReadAndWriteLock())

class SubscribedTopic:
    def __init__(self,name):
        self.name = name

class TopicNeighbor:
    def __init__(self,ip_address,topic,p2p_id):
        self.ip_address = ip_address
        self.p2p_id = p2p_id
        self.topic = topic

class Post:
    def __init__(self, topic,ip_address,text,timestamp,hash_id):
        self.topic = topic
        self.ip_address = ip_address
        self.text = text
        self.timestamp = timestamp
        self.hash_id = hash_id


# LEADER
# set the leader
def set_leader(ip_address,p2p_id):
    rw_locks["leader"].acquire_writelock()
    global_var["leader"] = (ip_address,p2p_id)
    rw_locks["leader"].release_writelock()

# get the leader
# return ip address of the leader
def get_leader():
    rw_locks["leader"].acquire_readlock()
    leader_info = global_var["leader"]
    rw_locks["leader"].release_readlock()
    if leader_info == None:
        app.logger.debug("no leader")
        return None
    else:
        return leader_info

# SubscribedTopic
@app.route('/create-subscribed-topic', methods=['POST'])
def http_create_subscribed_topic():
    name = str(request.json.get('name'))
    create_subscribed_topic(name)
    if name == "" or name == None:
        return ({"error":"invalid name"})
    else:
        return jsonify({"message": f'created {name}'})

# create topic
def create_subscribed_topic(name):
    rw_locks["topic"].acquire_writelock()
    topic = subscribed_topics_db.find_one({'name':name})
    if topic is not None:
        rw_locks["topic"].release_writelock()
        app.logger.debug("topic already exists")
        return False
    new_topic = SubscribedTopic(name=name)
    topic_id = subscribed_topics_db.insert_one(new_topic.__dict__).inserted_id
    rw_locks["topic"].release_writelock()
    return True

# get subscribed topics
def get_subscribed_topics():
    rw_locks["topic"].acquire_readlock()
    topics = subscribed_topics_db.find()
    rw_locks["topic"].release_readlock()   
    return [topic["name"] for topic in topics]

# get specific subscribed topic
def get_subscribed_topic(name):
    rw_locks["topic"].acquire_readlock()
    topic = subscribed_topics_db.find_one({'name':name})
    rw_locks["topic"].release_readlock()   
    return topic

# delete subscribed topic
# returns whether deleted
def delete_subscribed_topic(name):
    rw_locks["topic"].acquire_writelock()
    result = subscribed_topics_db.delete_one({'name': name})
    if result.deleted_count != 1:
        app.logger.debug("topic not found")
    rw_locks["topic"].release_writelock()

# TopicNeighbor
# create topic neighbor
@app.route('/create-topic-neighbor', methods=['POST'])
def http_create_topic_neighbor():
    ip_address = str(request.json.get('ip_address'))
    topic = str(request.json.get('topic'))
    p2p_id = str(request.json.get('p2p_id'))
    return jsonify({"message": str(create_topic_neighbor(ip_address,topic,p2p_id))})

def create_topic_neighbor(ip_address,topic,p2p_id):
    rw_locks["topic-neighbor"].acquire_writelock()
    new_neighbor = TopicNeighbor(ip_address=ip_address,topic=topic,p2p_id=p2p_id)
    topic_id = topic_neighbors_db.insert_one(new_neighbor.__dict__).inserted_id
    rw_locks["topic-neighbor"].release_writelock()
    return str(topic_id)

@app.route('/get-all-topic-neighbors')
def get_all_topic_neighbors():
    entries = topic_neighbors_db.find()
    neighbors = [str(entry) for entry in entries]
    return jsonify({"neighbors":str(neighbors)})

# get topic neighbors
# return list of ip addresses
def get_topic_neighbors(topic):
    rw_locks["topic-neighbor"].acquire_readlock()
    neighbors = topic_neighbors_db.find({'topic':topic})
    rw_locks["topic-neighbor"].release_readlock()
    return [neighbor["ip_address"] for neighbor in neighbors]

# gets ip addresses of each topic neighbor
# return list of ip addresses
def get_topic_neighbors_from_all_topics():
    rw_locks["topic-neighbor"].acquire_readlock()
    neighbors = topic_neighbors_db.find()
    rw_locks["topic-neighbor"].release_readlock()
    return list(set([neighbor["ip_address"] for neighbor in neighbors]))

# gets p2p_ids of each topic neighbor
# return list of p2p_ids
def get_p2pids_of_all_neighbors():
    rw_locks["topic-neighbor"].acquire_readlock()
    neighbors = topic_neighbors_db.find()
    rw_locks["topic-neighbor"].release_readlock()
    neighbor_hash = dict()
    for neighbor in neighbors:
        neighbor_hash[neighbor["ip_address"]] = neighbor["p2p_id"]
    return neighbor_hash

# get topic neighbors
# return list of (ip address,p2p_id)
def get_tuples_from_topic_neighbors(topic):
    rw_locks["topic-neighbor"].acquire_readlock()
    neighbors = topic_neighbors_db.find({'topic':topic})
    rw_locks["topic-neighbor"].release_readlock()
    return [(neighbor["ip_address"],neighbors["p2p_id"]) for neighbor in neighbors]

# delete a neighbor from all topics
def delete_neighbor_from_all_topics(ip_address):
    rw_locks["topic-neighbor"].acquire_writelock()
    result = topic_neighbors_db.delete_many({'ip_address':ip_address})
    rw_locks["topic-neighbor"].release_writelock()

# delete all neighbors of from a topics
def delete_all_neighbors_from_a_topic(topic):
    rw_locks["topic-neighbor"].acquire_writelock()
    results = topic_neighbors_db.delete_many({'topic':topic})
    rw_locks["topic-neighbor"].release_writelock()

# Post
# create post
def create_post(topic,ip_address,text,timestamp,hash_id):
    rw_locks["post"].acquire_writelock()
    new_post = Post(topic=topic,ip_address=ip_address,text=text,timestamp=timestamp,hash_id=hash_id)
    post_id = posts_db.insert_one(new_post.__dict__).inserted_id
    app.logger.debug
    rw_locks["post"].release_writelock()

# get posts by topic
def get_posts_by_topic(topic):
    rw_locks["post"].acquire_readlock()
    posts = posts_db.find({'topic':topic})
    rw_locks["post"].release_readlock()
    return [post for post in posts]

#get post by hashid
def get_post(hash_id):
    rw_locks["post"].acquire_readlock()
    post = posts_db.find_one({'hash_id': hash_id})
    rw_locks["post"].release_readlock()
    return post

'''
# example of a function scheduled periodically with scheduler
def print_job():
    app.logger.debug("print job")
    app.logger.debug(global_var["ip_address"])
    app.logger.debug(global_var["p2p_id"])

    if not global_var["turn"]:
        app.logger.debug("create tom hanks user")
        create_user(name="Tom Hanks", email="tomhanks@gmail.com")

        global_var["turn"] = True
    else:
        users = get_users()
        app.logger.debug(users)

        global_var["turn"] = False
'''

'''
given a request call, attempt_request attempts to
send request call several times and retries 5 seconds
after failure. attempt_request attempts to send request
call every RETRY_COUNT times.
'''
def attempt_request(request_func):
    # initial number of attempts
    attempts = RETRY_COUNT
    while attempts > 0:
        # attempt to send request
        try:
            response = request_func()
            response.raise_for_status() 
            app.logger.debug("request successful!")
            return response
        # if error when sending request
        except requests.exceptions.RequestException as e:
            app.logger.error(f"request attempt {RETRY_COUNT - attempts} failed")
            app.logger.error(f"{e}")
            # reduces attempt count by 1
            attempts = attempts - 1
            # if more attempts, retries in 5 seconds
            if attempts > 0:
                app.logger.error("retrying in 5 seconds")
                time.sleep(5)    
    return None


# join network and update ["p2p_id"]
def join_network():
    # get current ip address
    global_var["ip_address"] = str(socket.gethostbyname(socket.gethostname()))
    app.logger.debug("ip address: " + str(global_var["ip_address"]))
    # create values that will be sent to backend-pod as parameters
    args = {
        "ip_address": global_var["ip_address"]
    }

    # send post request to backend-pod via backend-service with 5 retries
    response = attempt_request(lambda: requests.post("http://backend-service:5000/join-network",json=args))
    # if no response, exit
    if response == None:
        sys.exit()

    # get value from response
    global_var["p2p_id"] = str(response.json().get("p2p_id"))
    app.logger.debug("p2p_id: " + str(global_var["p2p_id"]))

    app.logger.debug("p2p node joined network")

# retrieve topics and update to global_var["topics"]
def get_topics():
    # send get request to backend-pod via backend-service
    response = attempt_request(lambda: requests.get("http://backend-service:5000/get-topics"))
    
    if response == None:
        # empty topics
        global_var["topics"] = None
        return

    # get topics
    global_var["topics"] = response.json().get("topics")

    app.logger.debug("p2p node retrieved topics:")
    app.logger.debug(f'{global_var["topics"]}')

#send posts to the nodes
@app.route('/send_post', methods=['POST'])
def http_send_post():
    topic = str(request.json.get('topic'))
    text = str(request.json.get('text'))
    return jsonify({"message": str(send_post(topic,text))})

# create hash id based on ip address, topic, text, and timestamp
def generate_unique_id(ip_address, topic,text,timestamp):
    concatenated_string = ip_address + topic + text + str(timestamp)
    hash_object = hashlib.sha256(concatenated_string.encode())
    # Get the hexadecimal representation of the hash
    unique_id = str(hash_object.hexdigest())
    return unique_id

def send_post(topic,text):
    timestamp = time.time()
    hash_id = generate_unique_id(global_var["ip_address"], topic,text,timestamp)
    post_id = create_post(topic,global_var["ip_address"],text,timestamp,hash_id)
    post = posts_db.find_one({"_id": post_id})
    if topic == "system":
        neighbors = get_topic_neighbors_from_all_topics()
    else:
        neighbors = get_topic_neighbors(topic)
    for neighbor in neighbors:
        args = {
            "author_ip_address": global_var["ip_address"],
            "sender_ip_address": global_var["ip_address"],
            "text": text,
            "hash_id": hash_id,
            "timestamp": str(timestamp),
            "topic": topic,
        }
        url = f"http://{neighbor}:5000/relay_post"
        response = attempt_request(lambda: requests.post(url,json=args))
        
        if response is None:
            app.logger.debug(f"{neighbor} did not receive the post related to the topic{topic}")
    app.logger.debug(f"finished sending post related to the topic{topic}")


@app.route('/relay_post', methods=['POST'])
def http_relay_post():
    data = request.get_json()
    hash_id = data.get('hash_id')
    topic = data.get('topic')
    existing_post = get_post(hash_id)
    app.logger.debug("this was retrieved from the database of the receiving p2pnode:" + str(existing_post))
    app.logger.debug("database of receiving p2p node of current topic: " + str(get_posts_by_topic(topic)))
    if existing_post is not None:
        # A post with this post_id already exists, so the incoming post is a duplicate
        app.logger.debug("duplicate post received " + global_var["ip_address"])
        return jsonify({"message": "Duplicate post received"})
    # extract the rest of the post data
    sender_ip_address = data.get('sender_ip_address')
    author_ip_address = data.get('author_ip_address')
    text = data.get('text')
    timestamp = data.get('timestamp')
    topic = data.get('topic')
    create_post(topic,author_ip_address,text,timestamp,hash_id)
    #get neighors of the topic
    if topic == "system" or "delete_node":
        neighbors = get_topic_neighbors_from_all_topics()
    else:
        neighbors = get_topic_neighbors(topic)
    if topic == "delete_node":
        delete_neighbor_from_all_topics(text)
    for neighbor in neighbors:
        if neighbor == author_ip_address or sender_ip_address:
            continue
        args = {
            "sender_ip_address": global_var["ip_address"],
            "author_ip_address": author_ip_address,
            "text": text,
            "hash_id": hash_id,
            "timestamp": str(timestamp),
            "topic": topic
        }
        url = f"http://{neighbor}:5000/relay_post"
        response = attempt_request(lambda: requests.post(url,json=args))
        if response is None:
            app.logger.debug(f"{neighbor} did not receive the post related to the topic{topic}")
    # Send a response back to the originating node
    return jsonify({"message": "Post received and saved"})


def checking_backend():
    # get current learder's ip_address
    (ip_address,p2p_id) = get_leader()
    args = {
        "ip_address": ip_address,
        "message": "checking on backend server"
    }
    url = f"http://backend-service:5000/failure-ping"
    response = attempt_request(lambda: requests.post(url,json=args))
    
    if response is None:
        #No response from backend server, assuming server failure
        #update all p2p nodes about server failer
        app.logger.debug("Backend server haven't replyed back")
        send_post("system","Backend failed")
    app.logger.debug("Backend server responded")


@app.route('/failure-ping', methods=['POST'])
def failure_ping():
    return jsonify({"message": f"Message received and Acknowledged"})


def checking_random_node():
    ip_addresses = get_topic_neighbors_from_all_topics()
    random_ip_address = random.choice(ip_addresses)
    args = {
        "message": "checking on random node"
    }
    url = f"http://{random_ip_address}:5000/failure-ping"
    response = attempt_request(lambda: requests.post(url,json=args))
    if response is None:
        #No response from thenode, assuming node failure
        #update all p2p nodes about node failer
        app.logger.debug(f"node with ip address {random_ip_address} hasn't replyed back")
        delete_neighbor_from_all_topics(random_ip_address)
        (leader_ip_address,p2p_id) = get_leader()
        url = f"http://{leader_ip_address}:5000/failed-node"
        response = attempt_request(lambda: requests.post(url,json={"message":f"{random_ip_address}"}))
    else:
        app.logger.debug(f"Node {random_ip_address} responded")

@app.route('/failed-node', methods=['POST'])
def failed_node():
    topic = "delete_node"
    text = request.json.get('message')
    delete_neighbor_from_all_topics(text)
    return jsonify({"message": str(send_post(topic,text))})
    


# start leader election: bully algorithm
@app.route('/start-election', methods=['POST'])
def start_election():
    app.logger.debug("backed or other p2p node called an election")
    thread = threading.Thread(target=election)
    thread.start()
    return jsonify({'message': "election started" })

# actual election: bully algorithm
def election():
    rw_locks["election"].acquire_writelock()
    # STEP 1: see if current p2p_id is bigger than p2p_id of p2p neighbors
    # get p2p_id of current p2p node
    p2p_id = int(global_var["p2p_id"])

    # get p2p_ids of all other topic neighbors
    p2p_hash = get_p2pids_of_all_neighbors()

    # whether the p2p ids of neighbors are bigger than p2p of current p2p node
    neighbors_are_bigger = False

    # bigger_neighbors[ip_address] = p2p_id
    bigger_neighbors_hash = dict()

    # check if any of the p2p ids of the neighbors are bigger than the p2p id of this current node
    for ip_address, other_p2p_id in p2p_hash.items():
        other_p2p_id = int(other_p2p_id)

        if other_p2p_id > p2p_id:
            neighbors_are_bigger = True
            bigger_neighbors_hash[ip_address] = other_p2p_id

    # STEP 2: If the p2p ids of the p2p neighbors are bigger, send start election to them
    # call others
    if neighbors_are_bigger:
        app.logger.debug("p2p ids of neighbors are bigger")
        app.logger.debug("send start-election request to those neighbors")
        bigger_neighbors_list = list(bigger_neighbors_hash.items())
        # sort by p2p id
        bigger_neighbors_list.sort(key= lambda value:value[1])
        # reverse order
        bigger_neighbors_list.reverse()

        bigger_neighbor_responded = False

        for ip_address,p2p_id in bigger_neighbors_list:
            # send start election command to that ip address
            url = f"http://{ip_address}:5000/start-election"
            response = attempt_request(lambda: requests.post(url))

            if response is not None:
                app.logger.debug(f"{ip_address} responded and will perform election")
                rw_locks["election"].release_writelock()
                return
        app.logger.debug("none of the neighbors responded")

    # STEP 3: If the other p2p nodes are not candidates or did not respond
    # curent p2p node will elect itself
    # elect itself
    app.logger.debug("p2p node will elect itself")
    leader_ip_address = global_var["ip_address"]
    leader_p2p_id =global_var["p2p_id"]
    set_leader(leader_ip_address,leader_p2p_id)
    # send coordinator messages to neighbors
    thread = threading.Thread(target=send_coordinator_message,args=(leader_ip_address,leader_p2p_id,"",))
    thread.start()
    rw_locks["election"].release_writelock()

# send coordinator message to p2p node neighbors
def send_coordinator_message(leader_ip_address,leader_p2p_id,sender_ip_address):
    # assign
    ip_addresses = get_topic_neighbors_from_all_topics()
    app.logger.debug("p2p node send coordinator message to other nodes")
    for ip_address in ip_addresses:
        # do not send coordinator message to sender
        if sender_ip_address == ip_address:
            continue
        args = {
            "ip_address": leader_ip_address,
            "p2p_id": leader_p2p_id,
            "sender" : global_var["ip_address"],
        }
        url = f"http://{ip_address}:5000/relay-coordinator-message"
        response = attempt_request(lambda: requests.post(url,json=args))

        if response is None:
            app.logger.debug(f"{ip_address} did not receive coordinator message")
    app.logger.debug("finished sending coordinator messages")

# http request to relay coordinator messages
@app.route('/relay-coordinator-message',methods=['POST'])
def relay_coordinator_message():
    app.logger.debug("p2p node received relay-coordinator-message")
    leader_ip_address = request.json.get('ip_address')
    leader_p2p_id = request.json.get('p2p_id')
    current_leader_ip_address = get_leader()

    if current_leader_ip_address == leader_ip_address:
        message = f'{current_leader_ip_address} was already elected'
        app.logger.debug(message)
        return jsonify({"message":message})
    else:
        message = f'{current_leader_ip_address} will be elected'
        set_leader(leader_ip_address,leader_p2p_id)
        thread = threading.Thread(target=send_coordinator_message,args=(leader_ip_address,leader_p2p_id,global_var["ip_address"]))
        thread.start()
        return jsonify({"message":message})
    

@app.route('/unsubscribe', methods=['POST'])
def unsubscribe():
    # get topic from request
    topic = request.json.get('topic')

    # Retrieve the neighbors of the unsubscribing P2P node from the database (ip_address, p2p_id)
    # list of tuples of the node's neighbors of that topic
    neighbors_list = get_tuples_from_topic_neighbors(topic)
    # list of ip addresses of each neighbor of that topic
    neighbors_ip_addresses = [ ip_address for (ip_address, _) in neighbors_list]
    # set of ip address of each neighbor of that topic
    neighbors_set = set(neighbors_ip_addresses)

    # given ip address, it provides p2p_id
    neighbors_p2p = dict()
    # given ip address of neighbor, gives parent of neighbor
    parent_hash = dict()
    # given ip address of neighbor, provides list of ip addresses of subsequent neighbors of current neighbor
    neighbors_hash = dict()
    # fills in the values
    for (neighbor_ip_address,neighbor_p2p_id) in neighbors_list:
        neighbors_p2p[neighbor_ip_address] = neighbor_p2p_id
        parent_hash[neighbor_ip_address] = neighbor_ip_address

    # iterate each ip address of the neighbors
    # to get the subsequent neighbors
    for neighbor_ip_address in neighbors_ip_addresses:
        # requests the neighbors
        response = attempt_request(lambda: requests.post(f"http://{neighbor_ip_address}:5000/get-topic-neighbors-by-topic", json=args))
        app.logger.debug(f'neighbors of {neighbor_ip_address} for topic {topic}: ' + str(response))
        if response is not None:
            subsequent_neighbors = list(response.json().get("message"))
            #  stores neighbors into the hash
            neighbors_hash[neighbor_ip_address] = subsequent_neighbors

    # given ip address, finds the root
    def find_root(A):
        while A != parent_hash[A]:
            A = parent_hash[A]
        return A
    
    # iterate each neighbor ip address
    for neighbor in neighbors_list:
        for subsequent_neighbor in neighbors_hash[neighbor]:
            if subsequent_neighbor in neighbors_set:
                # join them A<->E
                rootA = find_root(subsequent_neighbor)
                rootB = find_root(neighbor)
                parent_hash[rootA] = rootB
    
    # list of all the roots 
    root_list = list()
    # adds the ip addresses of the root nodes
    for node, parent in parent_hash.items():
        if node == parent:
            root_list.add(node)

    # make adjacent nodes in root list neighbors
    for i in len(root_list) - 1:
        ip_address1 = root_list[i]
        ip_address2 = root_list[i+1]
        p2p_id1 = neighbors_p2p[ip_address1]
        p2p_id2 = neighbors_p2p[ip_address2]

        args = {
            "ip_address": ip_address2,
            "p2p_id" : p2p_id2,
            "topic" : topic
        }

        response = attempt_request(lambda: requests.post(f"http://{ip_address1}:5000/create-topic-neighbor", json=args))
        app.logger.debug(f'neighbors of {neighbor_ip_address} for topic {topic}: ' + str(response))

        args = {
            "ip_address": ip_address1,
            "p2p_id" : p2p_id1,
            "topic" : topic
        }
        response = attempt_request(lambda: requests.post(f"http://{ip_address2}:5000/create-topic-neighbor", json=args))
        app.logger.debug(f'neighbors of {neighbor_ip_address} for topic {topic}: ' + str(response))
    

    # After creating the neighbor connections, tell backend to unsubscribe current node from topic
    args = {
        'ip_address': global_var["ip_address"],
        'topic': topic
    }
    response = attempt_request(lambda: requests.post(f'http://{get_leader()[0]}/unsubscribe-node', json=args))
    if response is not None and response.json().get('status') == 'success':
        app.logger.debug('Node was unsubscribed')
        return jsonify({"message":'Node was unsubscribed'})
    else:
        app.logger.error('Node was not unsubscribed')
        return jsonify({"message":'Node was not unsubscribed'})

@app.route('/unsubscribe-node', methods=['POST'])
def unsubscribed_node():
    args = {
        'ip_address': request.json.get('ip_address'),
        'topic': request.json.get('topic')
    }
    response = attempt_request(lambda: requests.post(f'http://backend-service/unsubscribe-node', json=args))
    if response is not None and response.json().get('status') == 'success':
        app.logger.debug('Node was unsubscribed')
        return jsonify({"message":'Node was unsubscribed'})
    else:
        app.logger.error('Node was not unsubscribed')
        return jsonify({"message":'Node was not unsubscribed'})

@app.route('/create-topic', methods=['POST'])
def create_topic():
    topic = request.json.get('topic')
    create_subscribed_topic(topic)

    args = {
        "ip_address":global_var["ip_address"],
        "topic": topic
    }
    response = attempt_request(lambda: requests.post(f'http://{get_leader()[0]}/leader-create-topic', json=args))
    if response is not None and response.json().get('status') == 'success':
        app.logger.debug('Node created topic')
        return jsonify({"message":'Node created topic'})
    else:
        app.logger.error('Node did not create topic')
        return jsonify({"message":'Node did not create topic'})


@app.route('/leader-create-topic', methods=['POST'])
def leader_create_topic():
    args = {
        'ip_address': request.json.get('ip_address'),
        'topic': request.json.get('topic')
    }
    response = attempt_request(lambda: requests.post(f'http://backend-service/create-topic', json=args))
    if response is not None and response.json().get('status') == 'success':
        app.logger.debug('Node created topic')
        return jsonify({"message":'Node created topic'})
    else:
        app.logger.error('Node did not create topic')
        return jsonify({"message":'Node did not create topic'})


if __name__ == "__main__":
    # join network and get topics
    join_network()
    get_topics()

    # if no topics retrieved, exit
    if global_var["topics"] == None:
        sys.exit()

    scheduler.init_app(app)
    scheduler.start()

    # add job
    # jobs can be added and removed add any time
    # even in functions and route functions
    # scheduler.add_job(id='print_job', func=print_job, trigger='interval', seconds=10)


    # run flask app
    app.run(host="0.0.0.0", port=5000)