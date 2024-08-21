/**
 * Copyright 2023 Google LLC
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *      http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 */

resource "google_compute_instance" "private-linux-faulty" {
  project        = google_project.project.project_id
  depends_on     = [google_project_service.compute]
  name           = "private-linux-faulty"
  machine_type   = "e2-micro"
  zone           = var.zone
  desired_status = "RUNNING"
  network_interface {
    network    = "default"
    subnetwork = google_compute_subnetwork.private_subnet.id
  }

  tags = ["faulty-vpc-instance-private"]
  scheduling {
    preemptible       = true
    automatic_restart = false
  }

  boot_disk {
    initialize_params {
      image = "debian-cloud/debian-11" # Choose an image
    }
  }

  labels = {
    foo = "bar"
  }

}

# firewall configuration used for connectivity testing
resource "google_compute_firewall" "private-linux-egress-deny" {
  name    = "private-linux-egress-deny"
  network = "default"
  project = google_project.project.project_id

  priority = 930

  direction = "EGRESS"

  deny {
    protocol = "all"
  }

  target_tags = google_compute_instance.private-linux-faulty.tags
  depends_on  = [google_compute_instance.private-linux-faulty]
}
