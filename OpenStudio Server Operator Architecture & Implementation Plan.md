# **OpenStudio Server Kubernetes Operator: Architectural Blueprint & Implementation Plan**

## **1\. Executive Summary & Objectives**

OpenStudio Server is a distributed energy simulation platform running on Ruby/Rails (web), background orchestrators (web\_background), database engines (MongoDB), and simulation compute nodes (worker). Running OpenStudio Server on Kubernetes via Helm provides baseline orchestration, but long-running, resource-heavy simulation workloads frequently suffer from:

1. **Analysis Stagnation:** Analyses hanging in started state due to deadlocks or unhandled EnergyPlus errors.  
2. **Resource Exhaustion:** Worker memory leaks, lingering RServe/EnergyPlus zombie threads, and local disk space saturation on worker nodes.  
3. **NFS Volume Bloat:** High-volume simulation outputs (.sql, .html, .osm) exhausting Shared Persistent Volumes (NFS/EFS/GCS Fuse).  
4. **Manual Intervention Needs:** Operations teams having to manually SSH, issue REST calls to soft-stop/kill runs, or bounce worker deployment pods.

### **Objective**

Build a lightweight, Kubernetes-native **OpenStudio Operator** (using **Python Kopf** or **Go Kubebuilder**) that automates operational tasks, implements automated recovery and soft stops, dynamically scales workers, and handles lifecycle data archiving/pruning.

## **2\. System Architecture & Component Design**

The Operator sits alongside the standard Helm deployment (openstudio-server-helm), observing both the Kubernetes API and the OpenStudio REST/MongoDB state.

┌─────────────────────────────────────────────────────────────────────────┐  
│                       OpenStudio Server Operator                        │  
│                                                                         │  
│  ┌──────────────────────┐   ┌──────────────────────┐   ┌─────────────┐  │  
│  │ Analysis Timeout     │   │ Zombie Datapoint     │   │ Worker      │  │  
│  │ & Soft-Stop Manager  │   │ Requeue Watchdog     │   │ Recycler    │  │  
│  └──────────┬───────────┘   └──────────┬───────────┘   └──────┬──────┘  │  
│             │                          │                      │         │  
│  ┌──────────┴───────────┐   ┌──────────┴───────────┐   ┌──────┴──────┐  │  
│  │ Storage & Artifact   │   │ Web Background       │   │ KEDA / HPA  │  │  
│  │ Pruner (S3/GCS)      │   │ Health Monitor       │   │ Controller  │  │  
│  └──────────────────────┘   └──────────────────────┘   └─────────────┘  │  
└────────────────────────────────────┬────────────────────────────────────┘  
                                     │  
           ┌─────────────────────────┴─────────────────────────┐  
           ▼                                                   ▼  
┌──────────────────────┐                            ┌─────────────────────┐  
│ OpenStudio REST API  │                            │ Kubernetes API      │  
│  \- /analyses.json    │                            │  \- Deployments      │  
│  \- /data\_points.json │                            │  \- Pods             │  
│  \- /cluster.json     │                            │  \- Events & CRDs    │  
└──────────────────────┘                            └─────────────────────┘

## **3\. Custom Resource Definition (CRD) Specification**

The operator will be governed by a CRD named OpenStudioClusterManager. This exposes declarative policy controls to cluster administrators.

### **openstudioclustermanager\_crd.yaml**

apiVersion: apiextensions.k8s.io/v1  
kind: CustomResourceDefinition  
metadata:  
  name: openstudioclustermanagers.energy.nrel.gov  
spec:  
  group: energy.nrel.gov  
  versions:  
    \- name: v1alpha1  
      served: true  
      storage: true  
      schema:  
        openAPIV3Schema:  
          type: object  
          properties:  
            spec:  
              type: object  
              properties:  
                serverUrl:  
                  type: string  
                  description: "Internal K8s DNS URL for OpenStudio web service."  
                targetWorkerDeployment:  
                  type: string  
                  description: "Name of worker Deployment to manage/recycle."  
                targetWebBackgroundDeployment:  
                  type: string  
                  description: "Name of web\_background Deployment."  
                analysisPolicy:  
                  type: object  
                  properties:  
                    maxDurationMinutes:  
                      type: integer  
                      default: 180  
                    gracefulStopTimeoutMinutes:  
                      type: integer  
                      default: 15  
                    autoSoftStop:  
                      type: boolean  
                      default: true  
                datapointPolicy:  
                  type: object  
                  properties:  
                    maxDatapointRuntimeMinutes:  
                      type: integer  
                      default: 45  
                    maxAutoRequeues:  
                      type: integer  
                      default: 3  
                workerPolicy:  
                  type: object  
                  properties:  
                    recycleWorkerIntervalHours:  
                      type: integer  
                      default: 12  
                    recycleAfterAnalysis:  
                      type: boolean  
                      default: true  
                storagePolicy:  
                  type: object  
                  properties:  
                    archiveToS3:  
                      type: boolean  
                      default: false  
                    s3BucketName:  
                      type: string  
                    purgeCompletedNFSFiles:  
                      type: boolean  
                      default: true

## **4\. Operator Core Modules & Logic Flow**

### **Module 1: Analysis Lifecycle & Soft-Stop Manager**

* **Goal:** Prevent stuck simulations from clogging worker pods indefinitely.  
* **Logic Flow:**  
  1. Poll GET /analyses.json every 30 seconds.  
  2. For each analysis with status \== "started":  
     * Check runtime against spec.analysisPolicy.maxDurationMinutes.  
     * If limit exceeded:  
       1. Issue PUT /analyses/{id}/action with body {"action": "soft\_stop"}.  
       2. Log K8s Warning Event: AnalysisSoftStopped.  
       3. Wait for spec.analysisPolicy.gracefulStopTimeoutMinutes.  
       4. If status remains stopping, escalate to {"action": "kill"} or {"action": "hard\_stop"}.

### **Module 2: Zombie Datapoint Watchdog & Auto-Requeue**

* **Goal:** Detect individual stalled simulations caused by worker OOM or pod eviction.  
* **Logic Flow:**  
  1. Poll GET /data\_points.json?status=started.  
  2. Check updated\_at timestamps for each datapoint.  
  3. If elapsed time exceeds spec.datapointPolicy.maxDatapointRuntimeMinutes and requeue\_count \< maxAutoRequeues:  
     * Call POST /data\_points/{id}/requeue.  
     * Increment internal requeue tracker.  
     * Emit K8s Normal Event: DatapointRequeued.

### **Module 3: Worker Node Hygiene & Post-Run Recycler**

* **Goal:** Flush transient EnergyPlus memory leaks and local temporary scratch directories.  
* **Logic Flow:**  
  1. Listen for analysis status transition: started \-\> completed / stopped / failed.  
  2. If spec.workerPolicy.recycleAfterAnalysis \== true and no other active analysis is running:  
     * Perform a rolling restart of the worker deployment (kubectl rollout restart deployment/\<targetWorkerDeployment\>).  
     * Force-clear lingering temp files on the shared volume if configured.

### **Module 4: Artifact Archiver & NFS Storage Pruner**

* **Goal:** Prevent shared NFS storage (PV) from running out of disk space.  
* **Logic Flow:**  
  1. Upon analysis completion:  
     * Spawn an ephemeral K8s Job mounted to the NFS PV.  
     * Job streams analysis.zip and structured CSV/JSON results to Amazon S3 or Google Cloud Storage bucket.  
     * On upload success verification, trigger DELETE /analyses/{id} REST call or delete raw datapoint folders on NFS.

### **Module 5: Resque / web\_background Watchdog**

* **Goal:** Ensure the job distribution mechanism hasn't deadlocked.  
* **Logic Flow:**  
  1. Read active worker status from /cluster.json or query MongoDB job queues.  
  2. If queued data points exist \> 0, but 0 workers are processing for \> 10 minutes while worker pods are Running and healthy:  
     * Assume web\_background scheduler loop is stuck.  
     * Trigger pod restart for web\_background deployment.

## **5\. State Machine Diagrams**

### **Analysis State Automation**

\[ Queued \] ───\> \[ Started \] ───────────────────────────────────────────────┐  
                     │                                                      │  
                     ├─ (Exceeds SLA Timeout) ─\> \[ Soft Stop Triggered \]    │  
                     │                                   │                  │  
                     │                          (Grace Period Pass?)        │  
                     │                                /     \\               │  
                     │                             (Yes)    (No)            │  
                     │                               /        \\             │  
                     │                       \[ Stopped \]   \[ Hard Kill \]    │  
                     │                            │               │         │  
                     └────────────────────────────┴───────────────┴─────────┴─\> \[ Trigger Worker Recycle & Storage Prune \]

## **6\. Implementation Phasing & Roadmap**

### **Phase 1: MVP Controller (Week 1–2)**

* Setup Kopf/Kubebuilder framework and deploy base CRD.  
* Implement Analysis SLA monitor & automated REST **Soft-Stop** caller.  
* Implement basic K8s Event emissions (AnalysisSoftStopped, WorkerRecycled).

### **Phase 2: Datapoint Watchdog & Worker Hygiene (Week 3–4)**

* Implement zombie datapoint detection and auto-requeue logic.  
* Implement post-analysis rolling worker restarts.  
* Implement web\_background queue-stall detector.

### **Phase 3: Storage & Archival Automation (Week 5\)**

* Build S3/GCS export container & K8s Job generator module.  
* Implement NFS workspace cleanup and API object deletion routines.

### **Phase 4: Production Hardening & Autoscaling (Week 6\)**

* Integrate **KEDA (Kubernetes Event-driven Autoscaling)** targeting MongoDB queue size or custom metrics from /analyses/{id}/status.json.  
* Implement Prometheus metrics endpoint (/metrics) inside the operator to track total soft-stops, auto-requeues, and storage freed.  
* Restrict operator ServiceAccount RBAC permissions.

## **7\. RBAC & Security Deployment Specification**

The operator requires restricted K8s permissions.

### **operator\_rbac.yaml**

apiVersion: v1  
kind: ServiceAccount  
metadata:  
  name: openstudio-operator-sa  
  namespace: openstudio  
\---  
apiVersion: rbac.authorization.k8s.io/v1  
kind: Role  
metadata:  
  name: openstudio-operator-role  
  namespace: openstudio  
rules:  
  \# Manage Custom Resources  
  \- apiGroups: \["energy.nrel.gov"\]  
    resources: \["openstudioclustermanagers", "openstudioclustermanagers/status"\]  
    verbs: \["\*"\]  
  \# Manage Worker Deployments & Pod Restarts  
  \- apiGroups: \["apps"\]  
    resources: \["deployments"\]  
    verbs: \["get", "list", "watch", "patch", "update"\]  
  \- apiGroups: \[""\]  
    resources: \["pods", "events"\]  
    verbs: \["get", "list", "watch", "create", "patch"\]  
  \# Create Ephemeral Archival Jobs  
  \- apiGroups: \["batch"\]  
    resources: \["jobs"\]  
    verbs: \["get", "list", "watch", "create", "delete"\]  
\---  
apiVersion: rbac.authorization.k8s.io/v1  
kind: RoleBinding  
metadata:  
  name: openstudio-operator-rb  
  namespace: openstudio  
subjects:  
  \- kind: ServiceAccount  
    name: openstudio-operator-sa  
    namespace: openstudio  
roleRef:  
  kind: Role  
  name: openstudio-operator-role  
  apiGroup: rbac.authorization.k8s.io  
